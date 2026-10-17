#!/usr/bin/env python3
"""089：数据库定期自动备份。

- PostgreSQL：pg_dump 生成 .sql 备份
- SQLite：sqlite3 备份 API 安全拷贝（避免 WAL 中途写入损坏）
- 备份存到 backend/backups/，按日命名：lupin_YYYYMMDD_HHMMSS.{sql,db}
- 保留最近 30 天，超期自动删除
- 失败写日志并返回非零退出码（供 cron/CI 判断）

用法：
    python scripts/backup.py
    python scripts/backup.py --backup-dir /var/backups/lupin --retention-days 7
    python scripts/backup.py --db-url postgresql://user:pass@host:5432/lupin
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BACKUP_DIR = os.path.join(BASE_DIR, "backend", "backups")
DEFAULT_RETENTION_DAYS = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [backup] %(levelname)s: %(message)s",
)
log = logging.getLogger("backup")


def resolve_db_url(explicit: str | None) -> str:
    if explicit:
        url = explicit.strip()
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        return url
    # 与 backend/auth/database.py 保持一致的解析逻辑
    sys.path.insert(0, BASE_DIR)
    from backend.auth.database import resolve_database_url
    return resolve_database_url()


def _sqlite_path(url: str) -> str:
    return url[len("sqlite:///"):]


def backup_sqlite(db_path: str, dest: str) -> None:
    """用 sqlite3 备份 API 安全拷贝（比直接 cp 更防 WAL 中途写入）。"""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"SQLite 数据库文件不存在：{db_path}")
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def backup_postgres(url: str, dest: str) -> None:
    """pg_dump 自定义格式备份（pg_restore 可恢复）。"""
    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        raise RuntimeError("未找到 pg_dump，请安装 PostgreSQL 客户端工具")
    cmd = [pg_dump, "--format=custom", "--file=" + dest, url]
    # 不把 URL 打印到日志（可能含密码）
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"pg_dump 失败（exit={proc.returncode}）：{proc.stderr[-500:]}")


def prune_old_backups(backup_dir: str, retention_days: int) -> int:
    """删除超过保留期的备份，返回删除数量。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for name in os.listdir(backup_dir):
        path = os.path.join(backup_dir, name)
        if not os.path.isfile(path):
            continue
        if not (name.startswith("lupin_") and (name.endswith(".db") or name.endswith(".dump"))):
            continue
        mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
        if mtime < cutoff:
            os.remove(path)
            removed += 1
            log.info("删除过期备份：%s", name)
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description="089：数据库定期自动备份（PG/SQLite 双支持）")
    ap.add_argument("--db-url", default=None, help="数据库连接串，默认读取 DATABASE_URL/LUPIN_DB_PATH")
    ap.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR, help="备份目录")
    ap.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS,
                    help="备份保留天数，默认 30")
    args = ap.parse_args()

    try:
        os.makedirs(args.backup_dir, exist_ok=True)
        url = resolve_db_url(args.db_url)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        if url.startswith("sqlite"):
            db_path = _sqlite_path(url)
            dest = os.path.join(args.backup_dir, f"lupin_{ts}.db")
            log.info("SQLite 备份：%s -> %s", db_path, dest)
            backup_sqlite(db_path, dest)
        elif url.startswith("postgresql"):
            dest = os.path.join(args.backup_dir, f"lupin_{ts}.dump")
            log.info("PostgreSQL 备份 -> %s", dest)
            backup_postgres(url, dest)
        else:
            raise RuntimeError(f"不支持的数据库类型：{url.split(':')[0]}")

        size = os.path.getsize(dest)
        log.info("备份成功：%s（%.1f KB）", dest, size / 1024)

        removed = prune_old_backups(args.backup_dir, args.retention_days)
        log.info("保留期 %d 天，清理过期备份 %d 个", args.retention_days, removed)
        return 0
    except Exception as e:
        log.error("备份失败：%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
