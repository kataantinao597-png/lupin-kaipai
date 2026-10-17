"""S6.3：审核版本绑定——内容哈希计算与新鲜度校验。

- compute_content_hash：对作品最新分镜（version + 镜头快照）算 SHA256，
  创建审核时存入 reviews.content_hash。
- validate_freshness：查询审核结果时重算当前哈希，不一致则自动置 expired
  并返回 (False, 当前哈希)，调用方提示用户重新审核。

版本常量：
- RISK_RULES_VERSION：backend/risk.py 确定性规则版本，改规则时必须同步+1。
- PROMPT_VERSION：backend/prompts.py 风险审核 Prompt 版本，改 Prompt 时+1。
- model_version：实际调用的模型名（创建审核时传入或取当前配置）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..models import (
    REVIEW_STATUS_APPROVED,
    REVIEW_STATUS_EXPIRED,
    REVIEW_STATUS_IN_REVIEW,
    REVIEW_STATUS_PENDING,
    REVIEW_TRANSITIONS,
)

# S6.3：规则/Prompt 版本——改动对应代码时必须同步递增，否则旧审核不会失效
RISK_RULES_VERSION = "1"   # backend/risk.py：check_fact_grounding 等确定性规则
PROMPT_VERSION = "1"       # backend/prompts.py：风险审核 Prompt

# 内容变化即失效的"有效"状态
_ACTIVE_STATUSES = (
    REVIEW_STATUS_PENDING, REVIEW_STATUS_IN_REVIEW, REVIEW_STATUS_APPROVED,
)


def compute_content_hash(db: Session, work_id: str) -> tuple[str, Optional[str]]:
    """计算作品当前可审核内容的 SHA256。

    内容 = 最新分镜 version + 镜头快照（稳定 ID/顺序/锁定/时长/内容）。
    返回 (content_hash, storyboard_id)；无分镜时返回 ("", None)。
    """
    sb = db.scalar(
        select(models.Storyboard)
        .where(models.Storyboard.work_id == work_id)
        .order_by(models.Storyboard.version.desc(),
                  models.Storyboard.created_at.desc())
        .limit(1))
    if sb is None:
        return "", None
    shots = list(db.scalars(
        select(models.Shot)
        .where(models.Shot.storyboard_id == sb.id)
        .order_by(models.Shot.shot_no.asc())))
    snapshot = {
        "storyboard_id": sb.id,
        "storyboard_version": sb.version,
        "shots": [
            {
                "shot_id_stable": s.shot_id_stable or s.id,
                "shot_no": s.shot_no,
                "locked": bool(s.locked),
                "duration_seconds": s.duration_seconds or 0,
                "content": s.content_json or {},
            }
            for s in shots
        ],
    }
    digest = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                   default=str).encode("utf-8")).hexdigest()
    return digest, sb.id


def current_versions(model: Optional[str] = None) -> dict:
    """S6.3：本次审核绑定的规则/模型/Prompt 版本。"""
    if not model:
        try:
            from ..core.config import get_settings
            model = get_settings().LUPIN_MODEL or ""
        except Exception:  # noqa: BLE001
            model = ""
    return {
        "rule_version": RISK_RULES_VERSION,
        "model_version": model or "",
        "prompt_version": PROMPT_VERSION,
    }


def validate_freshness(db: Session,
                       review: models.Review) -> tuple[bool, str]:
    """S6.3：校验审核结果是否仍绑定当前内容。

    返回 (is_fresh, current_hash)：
    - 内容未变 → (True, hash)
    - 内容已变且审核处于"有效"状态 → 自动置 expired，返回 (False, 新hash)
    - 内容已变但审核已是终态（rejected/expired/changes_requested）
      → 不再流转，仅返回 (False, 新hash)
    """
    current_hash, _ = compute_content_hash(db, review.work_id)
    if review.content_hash == current_hash:
        return True, current_hash
    # 内容变了：有效状态自动失效
    if (review.status in _ACTIVE_STATUSES
            and REVIEW_STATUS_EXPIRED in REVIEW_TRANSITIONS.get(review.status, ())):
        review.status = REVIEW_STATUS_EXPIRED
        review.version = (review.version or 1) + 1
        db.flush()
    return False, current_hash
