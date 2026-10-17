#!/usr/bin/env python3
"""093：数据生命周期清理。

按 docs/DATA_LIFECYCLE.md 的策略执行：
- 回收站作品超过 30 天 → 硬删除（含关联版本快照）
- 备份文件超过 30 天 → 删除
- 日志文件超过 90 天 → 删除
- 草稿 90 天未修改 → 只提醒（不自动删），输出列表

用法：
    python scripts/cleanup.py --dry-run     # 只报告，不执行（默认）
    python scripts/cleanup.py --apply        # 真正执行清理
    python scripts/cleanup.py --apply --trash-days 30 --backup-days 30 --log-days 90
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

# 先初始化 backend.auth 包，避免 backend.models 的循环导入
import backend.auth  # noqa: F401,E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [cleanup] %(levelname)s: %(message)s",
)
log = logging.getLogger("cleanup")


def cleanup_trash(db, trash_days: int, dry_run: bool) -> int:
    """回收站超过 N 天的作品硬删除。"""
    from backend import models
    cutoff = datetime.now(timezone.utc) - timedelta(days=trash_days)
    old = (db.query(models.Work)
           .filter(models.Work.deleted_at.isnot(None),
                   models.Work.deleted_at < cutoff)
           .all())
    for w in old:
        log.info("硬删除回收站作品：%s（%s，删除于 %s）",
                 w.id, w.title, w.deleted_at)
        if not dry_run:
            db.query(models.WorkVersion).filter(
                models.WorkVersion.work_id == w.id).delete()
            db.delete(w)
    if not dry_run:
        db.commit()
    return len(old)


def cleanup_files(directory: str, days: int, patterns: tuple[str, ...],
                  dry_run: bool, label: str) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    removed = 0
    if not os.path.isdir(directory):
        log.info("%s目录不存在，跳过：%s", label, directory)
        return 0
    for name in os.listdir(directory):
        if not any(name.endswith(p) for p in patterns):
            continue
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
        if mtime < cutoff:
            log.info("删除过期%s：%s", label, name)
            if not dry_run:
                os.remove(path)
            removed += 1
    return removed


def report_stale_drafts(db, stale_days: int) -> list:
    """90 天未修改的草稿只提醒，不删除。"""
    from backend import models
    cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
    stale = (db.query(models.Work)
             .filter(models.Work.deleted_at.is_(None),
                     models.Work.updated_at < cutoff)
             .all())
    for w in stale:
        log.warning("草稿 %s（%s）已 %d 天未修改，建议确认是否保留",
                    w.id, w.title, stale_days)
    return stale


def main() -> int:
    ap = argparse.ArgumentParser(description="093：按生命周期策略清理数据（默认 dry-run 只报告）")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只报告）")
    ap.add_argument("--dry-run", action="store_true", help="只报告不执行（默认行为，显式声明）")
    ap.add_argument("--trash-days", type=int, default=30)
    ap.add_argument("--backup-days", type=int, default=30)
    ap.add_argument("--log-days", type=int, default=90)
    ap.add_argument("--stale-days", type=int, default=90)
    ap.add_argument("--backup-dir", default=os.path.join(BASE_DIR, "backend", "backups"))
    ap.add_argument("--log-dir", default=os.path.join(BASE_DIR, "backend", "logs"))
    args = ap.parse_args()
    dry_run = not args.apply

    if dry_run:
        log.info("dry-run 模式：只报告，不执行。加 --apply 真正清理。")
    try:
        from backend.auth.database import get_session_factory
        db = get_session_factory()()

        n_trash = cleanup_trash(db, args.trash_days, dry_run)
        n_backup = cleanup_files(args.backup_dir, args.backup_days,
                                 (".db", ".dump"), dry_run, "备份文件")
        n_log = cleanup_files(args.log_dir, args.log_days,
                              (".log",), dry_run, "日志文件")
        stale = report_stale_drafts(db, args.stale_days)
        db.close()

        log.info("完成：硬删除 %d 个过期回收站作品，清理 %d 个备份，%d 个日志，%d 个长期未改草稿待确认",
                 n_trash, n_backup, n_log, len(stale))
        return 0
    except Exception as e:
        log.error("清理失败：%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
