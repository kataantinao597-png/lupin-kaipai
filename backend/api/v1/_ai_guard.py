"""P0 安全修复：AI 生成接口的组织归属守卫。

漏洞（2026-10-20 实测复现）
--------------------------
`/api/ideas`、`/api/storyboard` 等老接口按 MVP 设计允许匿名调用
（`_optional_user` 拿不到身份时返回 None 而不抛 401），但 `org_id` 来自请求
（`X-Org-Id` 头 / `?org_id=` / 请求体），`get_ai_gateway` 直接拿它做预算归属。

结果：「匿名 + 别人的 org_id」可以替别人花 AI 预算。实测未登录
`POST /api/ideas?org_id=<他人组织>` 返回 200 并产生真实文案，该组织
`ai_budgets.used_cents` 从 75 分涨到 81 分；同一请求不传 org_id 则不扣任何组织。

修复规则（只收紧越权，不改变匿名可用性）
----------------------------------------
1. 请求未声称 org_id → 行为与修复前完全一致：匿名可用，网关不绑定组织，
   不扣任何组织预算（因此不构成越权，也不影响现有匿名流程与测试）。
2. 请求声称了 org_id → 必须登录且是该组织成员，否则 401（未登录）/ 404（非成员）。
   404 而非 403：沿用 rbac 的防枚举口径，不向无权请求方确认组织是否存在。
3. 头 / query / 请求体三处声称的值不一致 → 400 直接拒绝，避免"头写 A、体写 B"绕过。

调用方拿到返回的 org_id 后应调用 `gw.bind_org(org_id)`，保证「请求体里带的
org_id」与「网关记账用的 org_id」是同一个值（原来只有头/query 能进网关，
body 里的 org_id 只用于取事实、不参与记账，两边会不一致）。
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from ...auth.database import get_db
from ...auth.deps import get_current_user
from ...auth.models import User
from ...rbac import get_membership

ORG_HEADER = "X-Org-Id"


def optional_user(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[User]:
    """可选登录：能确定身份就返回用户，否则 None（不抛 401）。

    与 `get_current_user` 共用同一套解析逻辑（Bearer → refresh Cookie 回退），
    只是把 401 降级为 None。原实现散落在 `ideas.py`，此处统一，避免两处漂移。

    之所以手动解析 Authorization：直接调用 `get_current_user` 不经过它的
    `_bearer` 依赖，请求头里的 Bearer 会被跳过、只剩 Cookie 通路。
    """
    auth = request.headers.get("Authorization", "")
    creds = None
    if auth.lower().startswith("bearer "):
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=auth[7:].strip())
    try:
        return get_current_user(credentials=creds, request=request, db=db)
    except HTTPException:
        return None


def _claimed_org_ids(request: Request, body_org_id: Optional[str]) -> List[str]:
    """收集请求声称的组织 ID（去重、去空）。"""
    raw = (
        (request.headers.get(ORG_HEADER) or "").strip(),
        (request.query_params.get("org_id") or "").strip(),
        (body_org_id or "").strip() if isinstance(body_org_id, str) else "",
    )
    uniq: List[str] = []
    for v in raw:
        if v and v not in uniq:
            uniq.append(v)
    return uniq


def enforce_ai_org(
    request: Request,
    body_org_id: Optional[str],
    user: Optional[User],
    db: Session,
) -> str:
    """校验并在通过后返回本次请求绑定的 org_id（未声称则返回空串）。"""
    claimed = _claimed_org_ids(request, body_org_id)
    if not claimed:
        return ""
    if len(claimed) > 1:
        raise HTTPException(
            status_code=400,
            detail="组织标识不一致（请求头/查询参数/请求体），拒绝执行",
        )
    org_id = claimed[0]
    if user is None:
        raise HTTPException(status_code=401, detail="指定组织时需先登录")
    ms = get_membership(db, user.id, org_id)
    if ms is None:
        raise HTTPException(status_code=404, detail="组织不存在")
    return ms.org_id


def bind_gateway_org(gw, org_id: str) -> None:
    """把已校验的组织绑定到网关（空串表示匿名，不解绑已有值）。"""
    if org_id:
        gw.bind_org(org_id)
