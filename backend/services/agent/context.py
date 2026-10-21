"""Phase 2：Agent 上下文装配 + 用户级配额桩位。

设计依据：docs/agent-upgrade/PHASE1_DESIGN.md §1.3（缺失 4）、§7。
约束：
- 身份只从服务端传入的 AgentIdentity 取，不信任任何模型/客户端回传的 id。
- 上下文只读装配，不做任何写入。
- check_user_quota() 是桩位：用户级每日限额（普通用户每天 5 次）由另一 agent
  实现，本分支只留空函数 + TODO，不假设它已存在。
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


def check_user_quota(*, user_id: str, org_id: str, db: Optional[Session] = None) -> dict[str, Any]:
    """用户级每日限额检查 —— 桩位（stub）。

    TODO(Phase 7)：用户级每日限额（普通用户每天 5 次，2026-10-21 用户拍板）
    由另一 agent 实现。本函数只占位，不假设其实现已存在；联调时在这里对接，
    返回口径保持 {"enforced": bool, "allowed": bool, "remaining": int|None, ...}。

    当前行为：不限流（enforced=False），保证 Phase 2-6 流程可跑通。
    """
    return {
        "enforced": False,
        "allowed": True,
        "remaining": None,
        "reason": "stub: 用户级每日限额尚未接入（Phase 7 对接另一 agent 的实现）",
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
    quota = check_user_quota(user_id=identity.user_id, org_id=org_id, db=db)

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
