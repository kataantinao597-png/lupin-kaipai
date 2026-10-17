"""025：ORM → 领域实体的转换函数。

转换集中在这里，Repository 调用，不在 api/services 散写。
"""
from __future__ import annotations

from . import entities as E


def work_to_domain(orm) -> E.Work:
    """SQLAlchemy Work → 领域 Work。"""
    return E.Work(
        id=orm.id,
        org_id=orm.org_id,
        owner_id=orm.owner_id,
        title=orm.title or "",
        status=orm.status or "draft",
        deleted_at=orm.deleted_at,
        version=orm.version or 1,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


def fact_to_domain(orm) -> E.Fact:
    """SQLAlchemy Fact → 领域 Fact。"""
    return E.Fact(
        id=orm.id,
        org_id=orm.org_id,
        product_id=orm.product_id or "",
        content=orm.content or "",
        category=orm.category or "",
        source_type=orm.source_type or "human_statement",
        source_ref=orm.source_ref or "",
        status=orm.status or "pending",
        allow_use=bool(orm.allow_use),
        verified_by=orm.verified_by,
        verified_at=orm.verified_at,
        expires_at=orm.expires_at,
        version=orm.version or 1,
    )


def shot_to_domain(orm) -> E.Shot:
    """SQLAlchemy Shot → 领域 Shot。"""
    return E.Shot(
        id=orm.id,
        storyboard_id=orm.storyboard_id,
        shot_no=orm.shot_no or 1,
        shot_id_stable=orm.shot_id_stable,
        content=dict(orm.content_json or {}),
        duration_seconds=orm.duration_seconds or 0,
        locked=bool(getattr(orm, "locked", False)),
    )


def storyboard_to_domain(orm, shots: list | None = None) -> E.Storyboard:
    """SQLAlchemy Storyboard（+镜头列表）→ 领域 Storyboard。"""
    return E.Storyboard(
        id=orm.id,
        work_id=orm.work_id,
        version=orm.version or 1,
        title=orm.title or "",
        shots=[shot_to_domain(s) for s in (shots or [])],
    )
