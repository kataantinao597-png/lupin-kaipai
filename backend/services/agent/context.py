"""Phase 2：Agent 上下文装配 + 用户级配额（已接真实限额）。

设计依据：docs/agent-upgrade/PHASE1_DESIGN.md §1.3（缺失 4）、§7。
约束：
- 身份只从服务端传入的 AgentIdentity 取，不信任任何模型/客户端回传的 id。
- 上下文只读装配，不做任何写入。
- check_user_quota() 已接入真实每日限额（2026-10-22）：计数与豁免判定统一走
  backend/services/poster_quota.py，与 HTTP 生图接口同一口径，避免
  「上下文说还有额度、生图接口却 429」的漂移。
  上下文装配对配额是**宽容读**（读不到不拦 run），真正拦截在生图端点。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from ... import models
from ...repositories.fact_repository import FactRepository
from ...repositories.product_repository import ProductRepository
from ...repositories.work_repository import WorkRepository
from ..poster_quota import POSTER_IMAGE_DAILY_LIMIT, get_quota_safe


@dataclass(frozen=True)
class AgentIdentity:
    """服务端身份（由路由层 get_current_user + org 解析构造，Phase 3 接入）。"""
    user_id: str
    org_id: str
    role: str  # owner | admin | editor | reviewer | viewer


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fact_to_dict(f: models.Fact) -> dict[str, Any]:
    return {
        "id": f.id,
        "content": f.content,
        "category": f.category,
        "status": f.status,
        "product_id": f.product_id,
        "source_type": f.source_type,
        "expires_at": f.expires_at.isoformat() if f.expires_at else None,
    }


def _product_to_dict(p: models.Product) -> dict[str, Any]:
    return {
        "id": p.id,
        "code": getattr(p, "code", None),
        "name": getattr(p, "name", ""),
    }


def _work_to_dict(w: models.Work) -> dict[str, Any]:
    try:
        data = json.loads(w.data_json or "{}")
    except (json.JSONDecodeError, TypeError):
        data = {}
    return {
        "id": w.id,
        "title": w.title,
        "status": w.status,
        "version": w.version,
        "owner_id": w.owner_id,
        "data_keys": sorted(data.keys()) if isinstance(data, dict) else [],
    }


def check_user_quota(*, user_id: str, org_id: str,
                     db: Optional[Session] = None,
                     role: Optional[str] = None) -> dict[str, Any]:
    """用户级每日限额（海报 AI 生图）—— 真实读数，非桩位。

    返回口径（与 POSTER_IMAGE_DAILY_LIMIT 同源）：
    {"enforced": bool,       # 本次是否受限额约束（豁免角色为 False）
     "allowed": bool,        # 现在还能不能再生图
     "remaining": int|None,  # 今日剩余次数；None = 不受限/未知
     "limit": int|None, "used": int|None,
     "reason": str}

    宽容策略：db 缺失或用量表读不到（老库未迁移 0022）时按「不受限」返回并标记
    reason，不让上下文装配失败阻断整个 run；硬拦截由生图端点 enforce_quota() 负责。
    """
    q = get_quota_safe(db, user_id, org_id=org_id, role=role)
    # M4：未知额度（用量表不可用）不伪装成"不受限"：上下文不阻断 run
    # （allowed=True），但 reason 明确写"未知"，前端据此显示"额度未知"。
    if q.get("unknown"):
        return {
            "enforced": False,
            "allowed": True,
            "remaining": None,
            "limit": q.get("limit"),
            "used": None,
            "unknown": True,
            "reason": "配图额度未知（用量表暂不可用），提交时以后端校验为准",
        }
    limited = bool(q.get("limited"))
    remaining = q.get("remaining")
    allowed = (not limited) or (remaining is not None and remaining > 0)
    if not limited:
        # 角色豁免（limit=None 且无 reason）
        reason = str(q.get("reason") or "admin/owner 豁免每日限额")
    elif allowed:
        reason = f"今日剩余 {remaining}/{POSTER_IMAGE_DAILY_LIMIT} 次"
    else:
        reason = f"今日 {POSTER_IMAGE_DAILY_LIMIT} 次已用完，明日 8 点（北京时间）恢复"
    return {
        "enforced": bool(q.get("enforced")),
        "allowed": allowed,
        "remaining": remaining,
        "limit": q.get("limit"),
        "used": q.get("used"),
        "unknown": False,
        "reason": reason,
    }


def assemble_agent_context(
    db: Session,
    identity: AgentIdentity,
    work_id: Optional[str] = None,
    product_id: Optional[str] = None,
    fact_limit: int = 20,
) -> dict[str, Any]:
    """装配 Agent 上下文：用户/组织/作品/产品/可用事实。只读，不写入。

    作品/产品按 org_id 重读并确认归属；归属不符时对应字段为 None（不抛错，
    避免向调用方泄露跨组织对象存在性）。
    """
    org_id = identity.org_id
    quota = check_user_quota(user_id=identity.user_id, org_id=org_id,
                             db=db, role=identity.role)

    work = None
    if work_id:
        w = WorkRepository(db).get(work_id, org_id)
        if w is not None and w.deleted_at is None:
            work = _work_to_dict(w)

    product = None
    if product_id:
        p = ProductRepository(db).get(product_id, org_id)
        if p is not None:
            product = _product_to_dict(p)

    facts = [
        _fact_to_dict(f)
        for f in FactRepository(db).list_usable(org_id, product_id=product_id,
                                               limit=fact_limit)
    ]

    return {
        "assembled_at": _utcnow().isoformat(),
        "user": {"user_id": identity.user_id, "org_id": org_id,
                 "role": identity.role},
        "quota": quota,
        "work": work,
        "product": product,
        "facts": facts,
    }
