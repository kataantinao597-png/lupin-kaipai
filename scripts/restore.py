#!/usr/bin/env python3
"""090：数据库恢复演练。

- SQLite：把备份 .db 文件拷贝到 --target 指定的数据库路径
- PostgreSQL：pg_restore 恢复 .dump 到 --target 指定的连接串
- 恢复后做行数校验：对比备份库与目标库关键表行数
- 演练建议先恢复到临时库验证，确认无误再覆盖正式库

用法：
    # SQLite 恢复到指定路径
    python scripts/restore.py --backup backend/backups/lupin_20261018_000000.db \\
        --target /tmp/lupin_restore_test.db
    # PostgreSQL 恢复
    python scripts/restore.py --backup backend/backups/lupin_20261018_000000.dump \\
        --target postgresql://user:pass@host:5432/lupin_restored
    # 只做行数校验（不恢复）
    python scripts/restore.py --verify-only --backup <file> --target <db>
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import subprocess
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [restore] %(levelname)s: %(message)s",
)
log = logging.getLogger("restore")

# 行数校验的关键表
CHECK_TABLES = ["users", "organizations", "memberships", "works", "work_versions"]


def _sqlite_path(url_or_path: str) -> str:
    if url_or_path.startswith("sqlite:///"):
        return url_or_path[len("sqlite:///"):]
    return url_or_path


def sqlite_row_counts(db_path: str) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    counts: dict[str, int] = {}
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in CHECK_TABLES:
            counts[t] = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] \
                if t in tables else 0
    finally:
        conn.close()
    return counts


def pg_row_counts(url: str) -> dict[str, int]:
    psql = shutil.which("psql")
    if not psql:
        raise RuntimeError("未找到 psql，请安装 PostgreSQL 客户端工具")
    counts: dict[str, int] = {}
    for t in CHECK_TABLES:
        proc = subprocess.run(
            [psql, url, "-tA", "-c", f'SELECT COUNT(*) FROM "{t}"'],
            capture_output=True, text=True, timeout=60)
        counts[t] = int(proc.stdout.strip()) if proc.returncode == 0 else -1
    return counts


def row_counts(target: str) -> dict[str, int]:
    if target.startswith("postgresql"):
        return pg_row_counts(target)
    return sqlite_row_counts(_sqlite_path(target))


def restore_sqlite(backup: str, target_path: str) -> None:
    if not os.path.exists(backup):
        raise FileNotFoundError(f"备份文件不存在：{backup}")
    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
    # 先验证备份文件是合法 SQLite
    sqlite_row_counts(backup)
    shutil.copy2(backup, target_path)
    log.info("SQLite 恢复完成：%s -> %s", backup, target_path)


def restore_postgres(backup: str, target_url: str) -> None:
    pg_restore = shutil.which("pg_restore")
    if not pg_restore:
        raise RuntimeError("未找到 pg_restore，请安装 PostgreSQL 客户端工具")
    cmd = [pg_restore, "--clean", "--if-exists", "--dbname=" + target_url, backup]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
    if proc.returncode != 0:
        raise RuntimeError(f"pg_restore 失败（exit={proc.returncode}）：{proc.stderr[-500:]}")
    log.info("PostgreSQL 恢复完成：%s", backup)


def main() -> int:
    ap = argparse.ArgumentParser(description="090：数据库恢复演练（支持 --target）")
    ap.add_argument("--backup", required=True, help="备份文件路径（.db 或 .dump）")
    ap.add_argument("--target", required=True,
                    help="恢复目标：SQLite 文件路径 或 PostgreSQL 连接串")
    ap.add_argument("--verify-only", action="store_true",
                    help="只做行数校验，不执行恢复")
    ap.add_argument("--yes", action="store_true",
                    help="跳过恢复确认（非交互环境用）")
    args = ap.parse_args()

    try:
        is_pg_backup = args.backup.endswith(".dump")
        is_pg_target = args.target.startswith("postgresql")
        if is_pg_backup != is_pg_target:
            raise RuntimeError("备份类型与目标类型不匹配（.dump 只能恢复到 PostgreSQL，.db 只能恢复到 SQLite）")

        if not args.verify_only:
            if not args.yes:
                log.warning("将用备份覆盖目标：%s", args.target)
                log.warning("演练建议先恢复到临时库。如确认继续，加 --yes 参数")
                return 2
            if is_pg_target:
                restore_postgres(args.backup, args.target)
            else:
                restore_sqlite(args.backup, _sqlite_path(args.target))

        # 行数校验
        backup_counts = row_counts(args.backup) if not is_pg_backup else None
        target_counts = row_counts(args.target)
        log.info("目标库行数：%s", target_counts)
        if backup_counts is not None:
            log.info("备份库行数：%s", backup_counts)
            mismatch = {t: (backup_counts.get(t), target_counts.get(t))
                        for t in CHECK_TABLES
                        if backup_counts.get(t) != target_counts.get(t)}
            if mismatch:
                log.error("行数校验不一致：%s", mismatch)
                return 1
            log.info("行数校验通过：备份与目标一致")
        else:
            log.info("PG 备份为自定义格式，跳过与备份文件直接比对，仅报告目标库行数")
        return 0
    except Exception as e:
        log.error("恢复失败：%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
