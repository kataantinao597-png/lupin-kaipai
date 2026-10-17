"""S6.5：交付门禁配置与豁免服务。

- 门禁配置：delivery_gate_configs 表；表为空/缺失时回退到内置默认
  （与旧硬编码行为一致，保证老接口行为不变）。
- 豁免：waivers 表；有效豁免 = 未撤销且未过期。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..models import DeliveryGateConfig, Waiver


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# S6.5：内置默认门禁配置（与 0005 迁移种子、旧硬编码行为三方一致）。
# (gate_type, check_id, label, is_blocking, is_waivable,
#  conditional_blocking, sort_order)
DEFAULT_GATE_CONFIGS: tuple = (
    ("plan", "product", "产品资料", True, False, False, 10),
    ("plan", "facts", "可信事实", False, False, False, 20),
    ("plan", "storyboard", "创意与分镜", True, False, False, 30),
    ("plan", "duration", "时长与校验", True, False, False, 40),
    ("plan", "risks", "风险审核", True, False, False, 50),
    ("plan", "shooting", "现场拍摄", False, True, False, 60),
    ("plan", "feasibility", "可行性检查", False, True, True, 70),
    ("production", "product", "产品资料", True, False, False, 10),
    ("production", "facts", "可信事实", False, False, False, 20),
    ("production", "storyboard", "创意与分镜", True, False, False, 30),
    ("production", "duration", "时长与校验", True, False, False, 40),
    ("production", "risks", "风险审核", True, False, False, 50),
    ("production", "shooting", "现场拍摄", True, True, False, 60),
    ("production", "feasibility", "可行性检查", False, True, True, 70),
    ("final", "product", "产品资料", True, False, False, 10),
    ("final", "facts", "可信事实", False, False, False, 20),
    ("final", "storyboard", "创意与分镜", True, False, False, 30),
    ("final", "duration", "时长与校验", True, False, False, 40),
    ("final", "risks", "风险审核", True, False, False, 50),
    ("final", "shooting", "现场拍摄", True, True, False, 60),
    ("final", "feasibility", "可行性检查", False, True, True, 70),
)


def _cfg_to_dict(c: DeliveryGateConfig) -> dict:
    return {
        "gate_type": c.gate_type,
        "check_id": c.check_id,
        "label": c.label,
        "is_blocking": bool(c.is_blocking),
        "is_waivable": bool(c.is_waivable),
        "conditional_blocking": bool(c.conditional_blocking),
        "required_permission": c.required_permission or "",
        "sort_order": c.sort_order or 0,
        "updated_by": c.updated_by,
    }


def get_gate_configs(db: Session, gate_type: str) -> list[dict]:
    """取某交付类型的门禁配置（按 sort_order）。

    表不存在/无数据时回退内置默认，保证行为一致。
    """
    try:
        rows = list(db.scalars(
            select(DeliveryGateConfig)
            .where(DeliveryGateConfig.gate_type == gate_type)
            .order_by(DeliveryGateConfig.sort_order.asc())))
    except Exception:  # noqa: BLE001 — 表不存在（迁移未跑）时回退默认
        rows = []
    if not rows:
        return [
            {"gate_type": gt, "check_id": cid, "label": label,
             "is_blocking": b, "is_waivable": w,
             "conditional_blocking": cb, "required_permission": "",
             "sort_order": order, "updated_by": None}
            for (gt, cid, label, b, w, cb, order) in DEFAULT_GATE_CONFIGS
            if gt == gate_type
        ]
    return [_cfg_to_dict(c) for c in rows]


def get_gate_config(db: Session, gate_type: str,
                    check_id: str) -> Optional[dict]:
    for c in get_gate_configs(db, gate_type):
        if c["check_id"] == check_id:
            return c
    return None


def upsert_gate_config(db: Session, gate_type: str, check_id: str,
                       is_blocking: Optional[bool] = None,
                       is_waivable: Optional[bool] = None,
                       updated_by: str = "") -> dict:
    """管理员调整门禁配置（不存在则按默认创建后更新）。"""
    cfg = db.scalar(
        select(DeliveryGateConfig).where(
            DeliveryGateConfig.gate_type == gate_type,
            DeliveryGateConfig.check_id == check_id))
    if cfg is None:
        base = get_gate_config(db, gate_type, check_id) or {}
        cfg = DeliveryGateConfig(
            gate_type=gate_type, check_id=check_id,
            label=base.get("label", check_id),
            is_blocking=base.get("is_blocking", True),
            is_waivable=base.get("is_waivable", False),
            conditional_blocking=base.get("conditional_blocking", False),
            sort_order=base.get("sort_order", 0),
        )
        db.add(cfg)
        db.flush()
    if is_blocking is not None:
        cfg.is_blocking = is_blocking
    if is_waivable is not None:
        cfg.is_waivable = is_waivable
    cfg.updated_by = updated_by or None
    cfg.updated_at = _utcnow()
    db.flush()
    db.refresh(cfg)
    db.commit()
    return _cfg_to_dict(cfg)


def is_waiver_active(w: Waiver, now: Optional[datetime] = None) -> bool:
    """豁免是否有效：未撤销且未过期。"""
    if w.revoked:
        return False
    now = now or _utcnow()
    exp = w.expires_at
    if exp is not None:
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= now:
            return False
    return True


def _waiver_to_dict(w: Waiver) -> dict:
    return {
        "id": w.id, "work_id": w.work_id, "gate_type": w.gate_type,
        "check_id": w.check_id, "reason": w.reason,
        "approved_by": w.approved_by, "expires_at": w.expires_at,
        "revoked": bool(w.revoked), "created_at": w.created_at,
    }


def get_active_waivers(db: Session, work_id: str,
                       gate_type: str) -> list[dict]:
    """取作品在某交付类型下的有效豁免。"""
    rows = list(db.scalars(
        select(Waiver).where(
            Waiver.work_id == work_id,
            Waiver.gate_type == gate_type,
            Waiver.revoked.is_(False))))
    now = _utcnow()
    return [_waiver_to_dict(w) for w in rows if is_waiver_active(w, now)]


def list_waivers(db: Session, work_id: str,
                 include_revoked: bool = False) -> list[dict]:
    q = (select(Waiver).where(Waiver.work_id == work_id)
         .order_by(Waiver.created_at.desc()))
    if not include_revoked:
        q = q.where(Waiver.revoked.is_(False))
    return [_waiver_to_dict(w) for w in db.scalars(q)]


def create_waiver(db: Session, org_id: str, work_id: str, gate_type: str,
                  check_id: str, reason: str, approved_by: str,
                  expires_at: Optional[datetime] = None) -> dict:
    """创建豁免：reason 和 approved_by 必填（S6.5 审计要求）。"""
    if not (reason or "").strip():
        raise ValueError("豁免必须填写原因")
    if not (approved_by or "").strip():
        raise ValueError("豁免必须记录批准人")
    # 只有可豁免的检查项才能豁免
    cfg = get_gate_config(db, gate_type, check_id)
    if cfg is None:
        raise ValueError(f"未知检查项: {check_id}")
    if not cfg["is_waivable"]:
        raise ValueError(f"检查项 {check_id} 不可豁免")
    w = Waiver(org_id=org_id, work_id=work_id, gate_type=gate_type,
               check_id=check_id, reason=reason.strip(),
               approved_by=approved_by, expires_at=expires_at)
    db.add(w)
    db.flush()
    db.refresh(w)
    db.commit()
    return _waiver_to_dict(w)


def revoke_waiver(db: Session, waiver_id: str) -> dict:
    w = db.get(Waiver, waiver_id)
    if w is None:
        raise ValueError(f"豁免不存在: {waiver_id}")
    w.revoked = True
    db.flush()
    db.commit()
    db.refresh(w)
    return _waiver_to_dict(w)
