"""海报 AI 生图每日限额 —— 全项目唯一口径。

为什么单独一个模块：限额同时被「HTTP 生图接口」和「Agent 上下文」读取。
两处各写一份计数 SQL，改一处漏一处就会出现「上下文说还能出图、接口却 429」
这种漂移（相同数据存两处比不同数据更易遗漏）。所以计数与豁免判定只在这里。

规则（2026-10-22 用户拍板）：
- 普通用户（editor / reviewer / viewer）每天 5 次；
- admin / owner 豁免（不计数、不拦截，quota 返回 limited=False）；
- 计数口径：poster_image_usage 表按 user_id + 当天 UTC 0 点起算；
- 超限 → HTTP 429 + 业务码 AI_RATE_LIMITED，错误文案给出额度与重置口径。

所有函数都只读，不写台账：记一条用量由调用方在**成功出图后**写
（失败不扣额度，避免用户为上游 5xx 买单）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .. import models
from ..core import error_codes
from ..core.exceptions import BusinessError
from ..rbac import ROLE_ADMIN, ROLE_RANK

logger = logging.getLogger(__name__)

#: 普通用户海报生图每日限额
POSTER_IMAGE_DAILY_LIMIT = 5


def is_quota_exempt(role: Optional[str]) -> bool:
    """admin / owner 豁免每日限额。"""
    return bool(role) and ROLE_RANK.get(role, 0) >= ROLE_RANK[ROLE_ADMIN]


def today_start_utc() -> datetime:
    """当天 UTC 0 点（用量按自然日 UTC 归集）。"""
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def count_today(db: Session, user_id: str) -> int:
    """该用户今天已成功的生图次数。"""
    return int(db.query(func.count(models.PosterImageUsage.id))
                 .filter(models.PosterImageUsage.user_id == user_id,
                         models.PosterImageUsage.created_at >= today_start_utc())
                 .scalar() or 0)


def get_quota(db: Session, user_id: str, org_id: Optional[str] = None,
              role: Optional[str] = None) -> dict:
    """当前用户今日额度。返回 {limited, limit, used, remaining}。

    豁免角色：limited=False、limit/used/remaining 均为 None（表示"不受限"，
    与"用完 0 次"区分开，前端据此显示"不限"而不是"0/5"）。
    """
    if is_quota_exempt(role):
        return {"limited": False, "limit": None, "used": None,
                "remaining": None}
    used = count_today(db, user_id)
    return {"limited": True, "limit": POSTER_IMAGE_DAILY_LIMIT,
            "used": used,
            "remaining": max(0, POSTER_IMAGE_DAILY_LIMIT - used)}


def enforce_quota(db: Session, user_id: str, org_id: Optional[str] = None,
                  role: Optional[str] = None) -> dict:
    """超限抛 429（AI_RATE_LIMITED）；豁免直接返回额度。返回本次判定快照。"""
    q = get_quota(db, user_id, org_id, role)
    if q["limited"] and (q["remaining"] or 0) <= 0:
        raise BusinessError(
            f"今日海报生成次数已用完（普通用户每天 {POSTER_IMAGE_DAILY_LIMIT} 次），"
            f"明天 8 点（北京时间）后恢复",
            code=error_codes.AI_RATE_LIMITED,
            http_status=429,
        )
    return q


def get_quota_with_inflight(db: Session, user_id: str,
                            org_id: Optional[str] = None,
                            role: Optional[str] = None) -> dict:
    """S1 修复：有效额度 = 已成功次数 + 在途（running）任务数。

    只读校验、防并发 burst 的关键：异步提交只读校验、成功后才记账时，
    并发请求会在计数还是 0 时全部通过。把在途任务计入已用，提交瞬间
    的并发请求就能互相看见对方的占位。豁免角色不受影响。
    """
    q = get_quota(db, user_id, org_id, role)
    if not q["limited"]:
        q["inflight"] = 0
        return q
    # 函数内导入：poster_jobs 不依赖本模块，无循环导入；但保持配额模块
    # 对任务存储的依赖显式化（仅在需要时引入）。
    from . import poster_jobs
    inflight = poster_jobs.count_running_for_user(user_id)
    q["inflight"] = inflight
    q["used"] = (q["used"] or 0) + inflight
    q["remaining"] = max(0, POSTER_IMAGE_DAILY_LIMIT - q["used"])
    return q


def enforce_quota_with_inflight(db: Session, user_id: str,
                                org_id: Optional[str] = None,
                                role: Optional[str] = None) -> dict:
    """S1 修复：按有效额度（已成功 + 在途）拦截，超限抛 429。

    调用方须在配额锁内完成“校验 + 建任务”原子序列（见 poster_image
    的 _quota_submit_lock），否则校验与建任务之间的竞态仍未闭合。
    """
    from . import poster_jobs
    q = get_quota_with_inflight(db, user_id, org_id, role)
    if q["limited"] and (q["remaining"] or 0) <= 0:
        raise BusinessError(
            f"今日海报生成次数已用完（普通用户每天 {POSTER_IMAGE_DAILY_LIMIT} 次，"
            f"含 {q['inflight']} 个正在生成），"
            f"明天 8 点（北京时间）后恢复",
            code=error_codes.AI_RATE_LIMITED,
            http_status=429,
        )
    if q["limited"] and q["inflight"] >= poster_jobs.MAX_INFLIGHT_PER_USER:
        raise BusinessError(
            f"同时进行的生图任务过多（最多 {poster_jobs.MAX_INFLIGHT_PER_USER} 个），"
            f"请等当前任务完成后再提交",
            code=error_codes.AI_RATE_LIMITED,
            http_status=429,
        )
    return q


def get_quota_safe(db: Optional[Session], user_id: str,
                   org_id: Optional[str] = None,
                   role: Optional[str] = None) -> dict:
    """给「非拦截路径」（如 Agent 上下文装配）用的宽容版本。

    设计取舍：上下文装配失败不应该让整个 run 起不来，所以
    - db 为空 → 视为未知额度（enforced=False，不拦）；
    - 表不存在/查询异常（老库未迁移 0022）→ fail-open + 记 log，不拦。
    真正的拦截一律走 enforce_quota()，那里对异常是硬失败。

    M4 修复：未知额度必须返回 unknown=True，绝不能伪装成“不限”——
    否则前端会显示“配图不限次”，用户提交时却被 enforce_quota 429，
    展示与拦截口径不一致。
    """
    if db is None:
        return {"limited": None, "limit": None, "used": None,
                "remaining": None, "enforced": False,
                "unknown": True,
                "reason": "no_db_session"}
    try:
        q = get_quota(db, user_id, org_id, role)
    except SQLAlchemyError:
        logger.warning("生图额度读取失败（可能未迁移 0022），按未知处理",
                       exc_info=True)
        return {"limited": None, "limit": None, "used": None,
                "remaining": None, "enforced": False,
                "unknown": True,
                "reason": "quota_unavailable"}
    q["enforced"] = bool(q["limited"])
    q["unknown"] = False
    return q
