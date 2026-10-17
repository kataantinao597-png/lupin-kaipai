#!/usr/bin/env python3
"""097/098：localStorage 作品迁移到新版数据库。

读取用户从浏览器导出的 localStorage JSON（见下方导出方法），
导入到 works 表；facts 内嵌在 data_json 中，无需单独建表。

浏览器导出方法（控制台执行）：
    copy(localStorage.getItem("lupin_works_v4"))
把剪贴板内容保存为 works_export.json 后用本脚本导入。

用法：
    python scripts/migrate_localstorage.py --input works_export.json \\
        --org-id <组织ID> --user-id <你的user_id>
    python scripts/migrate_localstorage.py --input works_export.json \\
        --org-id <组织ID> --user-id <你的user_id> --report migration_report.json

校验报告（098）：输出 JSON，含缺失字段、重复 ID、非法分镜结构统计。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

# 先初始化 backend.auth 包，避免 backend.models 的循环导入
#（auth/__init__ → router → repositories → backend.models）
import backend.auth  # noqa: F401,E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [migrate] %(levelname)s: %(message)s")
log = logging.getLogger("migrate")


def ms_to_dt(ms) -> datetime:
    try:
        return datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def validate_item(item: dict, seen_ids: set) -> list[str]:
    """返回该作品的问题列表。"""
    issues: list[str] = []
    if not isinstance(item, dict):
        return ["item 不是对象"]
    wid = item.get("id")
    if not wid:
        issues.append("缺失字段：id")
    elif wid in seen_ids:
        issues.append(f"重复 ID：{wid}")
    if not item.get("name"):
        issues.append("缺失字段：name（将用默认标题）")
    data = item.get("data")
    if data is None:
        issues.append("缺失字段：data（将用空作品）")
    elif not isinstance(data, dict):
        issues.append("非法字段：data 不是对象")
    else:
        sb = data.get("storyboard")
        if sb is not None:
            if not isinstance(sb, dict):
                issues.append("非法分镜：storyboard 不是对象")
            elif "shots" in sb and not isinstance(sb["shots"], list):
                issues.append("非法分镜：shots 不是数组")
            else:
                for i, shot in enumerate(sb.get("shots") or []):
                    if not isinstance(shot, dict):
                        issues.append(f"非法分镜：第 {i} 个镜头不是对象")
                        break
                    if not shot.get("shot_id"):
                        issues.append(f"非法分镜：第 {i} 个镜头缺 shot_id（007 要求）")
                        break
        facts = data.get("facts")
        if facts is not None and not isinstance(facts, list):
            issues.append("非法字段：facts 不是数组")
    return issues


def migrate(input_path: str, org_id: str, user_id: str, report_path: str | None) -> int:
    from backend import models
    from backend.auth.database import get_session_factory

    with open(input_path, encoding="utf-8") as f:
        raw = json.load(f)

    # 兼容两种格式：{version, activeId, items:[...]} 或直接是 items 数组
    if isinstance(raw, dict) and isinstance(raw.get("items"), list):
        items = raw["items"]
    elif isinstance(raw, list):
        items = raw
    else:
        log.error("无法识别的导出格式：需要 {version, items:[...]} 或 [...]")
        return 1

    db = get_session_factory()()

    # 目标组织必须存在（works.org_id 有 FK 约束，086）
    org = db.query(models.Organization).filter(models.Organization.id == org_id).first()
    if org is None:
        log.error("组织 %s 不存在，请先创建组织再迁移（租户隔离要求）", org_id)
        db.close()
        return 1

    report = {
        "input": input_path,
        "total": len(items),
        "imported": 0,
        "skipped": 0,
        "issues": [],       # [{id, problems:[...]}]
        "duplicate_ids": [],
        "missing_fields": {},
        "invalid_storyboards": 0,
    }
    seen_ids: set[str] = set()

    for item in items:
        problems = validate_item(item, seen_ids)
        wid = item.get("id") if isinstance(item, dict) else None
        if wid:
            if wid in seen_ids:
                report["duplicate_ids"].append(wid)
            seen_ids.add(wid)

        fatal = any(p.startswith(("重复 ID", "item 不是对象", "非法字段：data 不是对象"))
                    for p in problems)
        if problems:
            entry = {"id": wid, "problems": problems}
            report["issues"].append(entry)
            for p in problems:
                if p.startswith("缺失字段"):
                    key = p.split("：")[1].split("（")[0]
                    report["missing_fields"][key] = report["missing_fields"].get(key, 0) + 1
                if p.startswith("非法分镜"):
                    report["invalid_storyboards"] += 1
        if fatal:
            report["skipped"] += 1
            log.warning("跳过作品 %s：%s", wid, "; ".join(problems))
            continue

        # 字段映射：旧格式 -> 新表
        data = item.get("data") or {}
        work = models.Work(
            org_id=org_id,
            owner_id=user_id,
            title=(item.get("name") or "未命名作品")[:256],
            data_json=json.dumps(data, ensure_ascii=False),
        )
        # 保留原始时间戳
        if item.get("createdAt"):
            work.created_at = ms_to_dt(item["createdAt"])
        if item.get("updatedAt"):
            work.updated_at = ms_to_dt(item["updatedAt"])
        db.add(work)
        report["imported"] += 1

    db.commit()
    db.close()

    log.info("迁移完成：共 %d，导入 %d，跳过 %d",
             report["total"], report["imported"], report["skipped"])
    if report_path:
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        log.info("校验报告已写入：%s", report_path)
    elif report["issues"]:
        log.info("问题明细：%s", json.dumps(report["issues"], ensure_ascii=False)[:500])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="097/098：localStorage 导出 JSON 迁移到数据库（含校验报告）")
    ap.add_argument("--input", required=True, help="浏览器导出的 JSON 文件")
    ap.add_argument("--org-id", required=True, help="目标组织 ID（租户隔离）")
    ap.add_argument("--user-id", required=True, help="导入者 user_id（记为 owner）")
    ap.add_argument("--report", default=None, help="校验报告 JSON 输出路径")
    args = ap.parse_args()
    try:
        return migrate(args.input, args.org_id, args.user_id, args.report)
    except Exception as e:
        log.error("迁移失败：%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
