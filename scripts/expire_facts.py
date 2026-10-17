#!/usr/bin/env python3
"""S6.1：事实过期扫库脚本。

将已过有效期的 confirmed 事实自动转 expired（operator=system，
写审计 + 发 FactChanged 事件）。

用法：
    ./venv/bin/python scripts/expire_facts.py [--org-id ORG_ID]

建议挂 cron 每天跑一次，例如：
    0 3 * * * cd /path/to/lupin-kaipai && ./venv/bin/python scripts/expire_facts.py

退出码：0=成功（输出过期条数），非 0=失败。
"""
from __future__ import annotations

import argparse
import logging
import sys

sys.path.insert(0, ".")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("expire_facts")


def main() -> int:
    ap = argparse.ArgumentParser(description="事实过期扫库（S6.1）")
    ap.add_argument("--org-id", default=None, help="只扫指定组织，不传则扫全库")
    args = ap.parse_args()

    try:
        # 先 import backend.auth，保证包初始化顺序（避开循环导入）
        import backend.auth  # noqa: F401
        from backend.auth.database import get_session_factory
        from backend.repositories.fact_repository import FactRepository
    except Exception as e:
        log.error("初始化失败：%s: %s", type(e).__name__, e)
        return 1

    db = get_session_factory()()
    try:
        n = FactRepository(db).expire_due(args.org_id)
    except Exception as e:
        log.error("扫库失败：%s: %s", type(e).__name__, e)
        return 1
    finally:
        db.close()
    log.info("过期扫库完成：%d 条事实转为 expired（org=%s）",
             n, args.org_id or "全部")
    return 0


if __name__ == "__main__":
    sys.exit(main())
