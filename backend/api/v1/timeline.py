"""分镜时间轴 API（S8.3）。

- GET /api/v1/works/{work_id}/timeline
  镜头时长总和 vs 作品目标时长差值提示。
  目标时长取 works.data_json → product.shoot.target_duration_seconds（默认 60s）；
  镜头时长取该作品最新分镜版本的 shots 表（无 DB 分镜时回退 data_json）。

返回：target/total/diff/是否在 2s 容差内 + 逐镜头时长明细。
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ... import models
from ...auth.database import get_db
from ...auth.deps import get_current_user
from ...auth.models import User
from ...core import error_codes
from ...core.exceptions import BusinessError
from ...core.config import get_settings
from ...core.response import ApiResponse, success
from ...rbac import assert_can, get_membership, has_permission
from ...repositories.storyboard_repository import StoryboardRepository

router = APIRouter(prefix="/works", tags=["v1-timeline"])


def _tolerance_seconds() -> float:
    """时长容差（秒），与 validator.validate_business 共用配置 LUPIN_DURATION_TOLERANCE。"""
    return get_settings().LUPIN_DURATION_TOLERANCE


class TimelineShot(BaseModel):
    shot_id: str = ""
    shot_no: int = 0
    shot_id_stable: Optional[str] = None
    duration_seconds: float = 0


class TimelineOut(BaseModel):
    work_id: str
    storyboard_id: Optional[str] = None
    target_duration_seconds: float
    total_duration_seconds: float
    diff_seconds: float = Field(description="total - target，正=超时，负=不足")
    within_tolerance: bool
    shot_count: int
    shots: list[TimelineShot] = Field(default_factory=list)


def _target_duration(work: models.Work) -> float:
    try:
        data = json.loads(work.data_json or "{}")
    except (ValueError, TypeError):
        data = {}
    try:
        target = (data.get("product") or {}).get("shoot", {}) \
            .get("target_duration_seconds", 60)
        return float(target) if float(target) > 0 else 60.0
    except (TypeError, ValueError):
        return 60.0


@router.get("/{work_id}/timeline", response_model=ApiResponse[TimelineOut])
def get_timeline(
    work_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """S8.3：分镜时间轴差值。需 work:read（成员身份，无权 404 防枚举）。"""
    work = (db.query(models.Work)
            .filter(models.Work.id == work_id,
                    models.Work.deleted_at.is_(None))
            .first())
    if work is None:
        raise BusinessError("作品不存在",
                            code=error_codes.WORK_NOT_FOUND, http_status=404)
    ms = get_membership(db, user.id, work.org_id)
    if ms is None or not has_permission(ms.role, "work:read"):
        raise BusinessError("作品不存在",
                            code=error_codes.WORK_NOT_FOUND, http_status=404)
    assert_can(ms.role, "work:read", "查看分镜时间轴")

    target = _target_duration(work)
    shots: list[TimelineShot] = []
    storyboard_id: Optional[str] = None

    # 优先 DB 分镜（最新版本）
    sbs = StoryboardRepository(db).list_by_work(work_id)
    if sbs:
        sb = sbs[0]
        storyboard_id = sb.id
        rows = (db.query(models.Shot)
                .filter(models.Shot.storyboard_id == sb.id)
                .order_by(models.Shot.shot_no.asc()).all())
        for r in rows:
            shots.append(TimelineShot(
                shot_id=r.id, shot_no=r.shot_no or 0,
                shot_id_stable=r.shot_id_stable,
                duration_seconds=float(r.duration_seconds or 0)))
    else:
        # 回退：data_json 里的 storyboard.shots
        try:
            data = json.loads(work.data_json or "{}")
            sb_json = (data.get("storyboard") or {})
            for i, s in enumerate(sb_json.get("shots") or [], start=1):
                shots.append(TimelineShot(
                    shot_id=str(s.get("id", "")),
                    shot_no=int(s.get("shot_number", i) or i),
                    shot_id_stable=s.get("shot_id_stable"),
                    duration_seconds=float(s.get("duration_seconds", 0) or 0)))
        except (ValueError, TypeError, AttributeError):
            pass

    total = sum(s.duration_seconds for s in shots)
    diff = total - target
    out = TimelineOut(
        work_id=work_id, storyboard_id=storyboard_id,
        target_duration_seconds=target,
        total_duration_seconds=total, diff_seconds=diff,
        within_tolerance=abs(diff) <= _tolerance_seconds(),
        shot_count=len(shots), shots=shots,
    )
    return success(out.model_dump())
