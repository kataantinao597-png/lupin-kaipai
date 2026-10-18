"""关键操作审计日志（Phase 9-5）。

审计事件写入 audit_events 表，供管理员查询（GET /api/v1/audit/events）。

记录的操作（action 常量）：
- api_key.create / api_key.revoke —— 密钥创建/撤销
- work.delete / work.hard_delete —— 作品软删除/硬删除
- org.member.set_role / org.member.remove —— 权限变更
- waiver.create / waiver.revoke —— 交付豁免批准/撤销

log_audit 只做 add + flush，不自己 commit：调用方回滚时审计一并回滚，
保证"操作与审计"原子性；flush 失败时记 warning，不破坏主流程。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .. import models

log = logging.getLogger("audit")


def log_audit(db: Session, *, action: str, org_id: str | None = None,
              user_id: str | None = None, object_type: str | None = None,
              object_id: str | None = None, detail: dict | None = None,
              ip: str | None = None) -> None:
    """写一条审计事件。绝不抛异常破坏主流程。"""
    try:
        db.add(models.AuditEvent(
            org_id=org_id,
            user_id=user_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            detail_json=detail or {},
            ip=ip,
            created_at=datetime.now(timezone.utc),
        ))
        db.flush()
    except Exception as e:  # noqa: BLE001 — 审计不能破坏业务
        log.warning("审计写入失败 action=%s: %s", action, e)


def client_ip(request) -> str | None:
    """取客户端 IP（注意：经过代理时为代理看到的地址）。"""
    try:
        return request.client.host if request.client else None
    except Exception:  # noqa: BLE001
        return None
