"""S6.1：事实引用守卫——AI 调用前检查引用事实状态。

expired / revoked / 未确认 / 不允许使用的事实不得被新生成内容引用。
供 AIGateway 调用前（runners.py）使用；与 risk.py 的 check_fact_grounding
（010，生成后检查）形成"调用前 + 生成后"双保险。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, TYPE_CHECKING

from sqlalchemy.orm import Session

from .state_machine import normalize_status, CONFIRMED

if TYPE_CHECKING:  # 类型注解用；运行时延迟导入以避开 backend.models 循环导入
    from .. import models as _models


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _models():
    """延迟导入 backend.models（模块级导入会触发 auth 包循环导入）。"""
    from .. import models
    return models


def extract_fact_ids(facts: Iterable[Any] | None) -> list[str]:
    """从前端传入的事实 dict 列表中提取事实 ID。

    无 id 的条目（纯前端自填文本）无法对库校验，跳过——它们不走事实库
    引用路径，生成后的 grounding 检查（010）仍会覆盖内容层风险。
    """
    ids: list[str] = []
    for f in facts or []:
        if isinstance(f, dict):
            fid = f.get("id")
            if fid:
                ids.append(str(fid))
        elif isinstance(f, str) and f.strip():
            ids.append(f.strip())
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for fid in ids:
        if fid not in seen:
            seen.add(fid)
            out.append(fid)
    return out


class UnusableFactError(ValueError):
    """引用的事实不可用（不存在/未确认/不允许使用/已过期/已撤销）。"""

    def __init__(self, fact_ids: list[str], detail: str = ""):
        self.fact_ids = fact_ids
        super().__init__(
            "以下事实不可作为生成依据"
            + (f"：{detail}" if detail else "")
            + f"（fact_ids={','.join(fact_ids)}）")


def check_facts_usable(db: Session, org_id: str,
                       fact_ids: list[str]) -> dict[str, "_models.Fact"]:
    """检查一批事实 ID 是否全部可用。

    可用 = 存在（本组织）+ status=confirmed + allow_use=true + 未过期
    （expires_at 为空或在未来）。任一不满足抛 UnusableFactError，
    调用方应在 AIGateway 调用前执行（S6.1）。

    返回 {fact_id: Fact} 供调用方继续使用。
    """
    models = _models()
    ids = [str(i) for i in (fact_ids or []) if str(i).strip()]
    if not ids:
        return {}
    now = _utcnow()
    rows = (db.query(models.Fact)
            .filter(models.Fact.org_id == org_id,
                    models.Fact.id.in_(ids))
            .all())
    by_id = {f.id: f for f in rows}
    bad: list[str] = []
    reasons: list[str] = []
    for fid in ids:
        f = by_id.get(fid)
        if f is None:
            bad.append(fid)
            reasons.append(f"{fid}: 不存在或无权访问")
            continue
        st = normalize_status(f.status)
        if st != CONFIRMED or f.allow_use is not True:
            bad.append(fid)
            reasons.append(
                f"{fid}: 状态={st}（需 confirmed 且 allow_use=true）")
            continue
        if f.expires_at is not None:
            exp = f.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp <= now:
                bad.append(fid)
                reasons.append(f"{fid}: 已过有效期")
                continue
    if bad:
        raise UnusableFactError(bad, "；".join(reasons))
    return by_id


def assert_facts_usable(db: Session, org_id: str,
                        facts: Iterable[Any] | None) -> dict[str, "_models.Fact"]:
    """便捷入口：从事实 dict 列表提取 ID 并检查。"""
    return check_facts_usable(db, org_id, extract_fact_ids(facts))
