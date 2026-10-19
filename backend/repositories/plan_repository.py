"""V3.0 C：内容策划工作台 Repository。

ContentTask 记录"营销目标→候选创意→选中→关联拍摄项目"流程。
关联拍摄项目只是记录 work_id 关系，不改写 work 已有内容。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .base import EntityNotFound, transactional_method


class ContentTaskNotFoundError(EntityNotFound):
    """策划任务不存在或不属于该组织。"""

    def __init__(self, task_id: str):
        super().__init__("策划任务", task_id)


class PlanRepository:
    def __init__(self, db: Session):
        self.db = db

    @transactional_method
    def create(self, org_id: str, created_by: str = "", **fields) -> models.ContentTask:
        goal = fields.get("content_goal", "brand")
        if goal not in models.PLAN_GOALS:
            raise ValueError(f"不支持的内容目标: {goal}")
        task = models.ContentTask(
            org_id=org_id,
            title=(fields.get("title") or "").strip() or "未命名策划",
            content_goal=goal,
            product_id=fields.get("product_id") or None,
            audience=(fields.get("audience") or "").strip(),
            content_purpose=(fields.get("content_purpose") or "").strip(),
            core_message=(fields.get("core_message") or "").strip(),
            selling_points=fields.get("selling_points") or [],
            style=(fields.get("style") or "").strip(),
            target_duration_seconds=int(fields.get("target_duration_seconds") or 30),
            target_platforms=fields.get("target_platforms") or [],
            shoot_conditions=fields.get("shoot_conditions") or {},
            ad_restrictions=(fields.get("ad_restrictions") or "").strip(),
            created_by=created_by or "",
        )
        self.db.add(task)
        self.db.flush()
        return task

    def get(self, org_id: str, task_id: str) -> models.ContentTask:
        task = self.db.execute(
            select(models.ContentTask).where(
                models.ContentTask.id == task_id,
                models.ContentTask.org_id == org_id)
        ).scalar_one_or_none()
        if task is None:
            raise ContentTaskNotFoundError(f"策划任务不存在: {task_id}")
        return task

    def list_by_org(self, org_id: str, goal: str | None = None,
                    status: str | None = None,
                    limit: int = 50) -> list[models.ContentTask]:
        q = select(models.ContentTask).where(
            models.ContentTask.org_id == org_id)
        if goal:
            q = q.where(models.ContentTask.content_goal == goal)
        if status:
            q = q.where(models.ContentTask.status == status)
        q = q.order_by(models.ContentTask.updated_at.desc()).limit(limit)
        return list(self.db.execute(q).scalars().all())

    @transactional_method
    def update_settings(self, org_id: str, task_id: str,
                        fields: dict) -> models.ContentTask:
        """更新策划输入（标题/目标/受众/风格/时长/平台/实拍条件/限制等）。"""
        task = self.get(org_id, task_id)
        if "content_goal" in fields and fields["content_goal"]:
            if fields["content_goal"] not in models.PLAN_GOALS:
                raise ValueError(f"不支持的内容目标: {fields['content_goal']}")
            task.content_goal = fields["content_goal"]
        for k in ("title", "audience", "content_purpose", "style",
                  "ad_restrictions"):
            if k in fields and fields[k] is not None:
                setattr(task, k, str(fields[k]).strip())
        if "core_message" in fields and fields["core_message"] is not None:
            task.core_message = str(fields["core_message"]).strip()
        if "selling_points" in fields and fields["selling_points"] is not None:
            task.selling_points = list(fields["selling_points"])
        if "target_platforms" in fields and fields["target_platforms"] is not None:
            task.target_platforms = list(fields["target_platforms"])
        if "shoot_conditions" in fields and fields["shoot_conditions"] is not None:
            task.shoot_conditions = dict(fields["shoot_conditions"])
        if "target_duration_seconds" in fields:
            task.target_duration_seconds = int(
                fields["target_duration_seconds"] or 30)
        if "product_id" in fields:
            task.product_id = fields["product_id"] or None
        if "status" in fields and fields["status"]:
            if fields["status"] not in (
                    models.PLAN_STATUS_DRAFT, models.PLAN_STATUS_PLANNED,
                    models.PLAN_STATUS_LINKED, models.PLAN_STATUS_ARCHIVED):
                raise ValueError(f"不支持的策划状态: {fields['status']}")
            task.status = fields["status"]
        self.db.flush()
        return task

    @transactional_method
    def save_candidates(self, org_id: str, task_id: str,
                        candidates: list, ai_meta: dict | None = None
                        ) -> models.ContentTask:
        task = self.get(org_id, task_id)
        task.candidates = list(candidates)
        # 重新生成后旧选中失效（选中是按索引的，候选变了必须重选）
        task.selected_index = None
        task.selected_idea = None
        if ai_meta:
            task.ai_meta = dict(ai_meta)
        if task.status == models.PLAN_STATUS_DRAFT:
            task.status = models.PLAN_STATUS_PLANNED
        self.db.flush()
        return task

    @transactional_method
    def select_idea(self, org_id: str, task_id: str,
                    index: int) -> models.ContentTask:
        task = self.get(org_id, task_id)
        cands = task.candidates or []
        if not cands:
            raise ValueError("还没有候选创意，请先生成")
        if not (0 <= index < len(cands)):
            raise ValueError(f"创意序号越界: {index}")
        task.selected_index = index
        task.selected_idea = dict(cands[index])
        self.db.flush()
        return task

    @transactional_method
    def link_work(self, org_id: str, task_id: str,
                  work_id: str) -> models.ContentTask:
        task = self.get(org_id, task_id)
        if task.selected_idea is None:
            raise ValueError("请先选择一个创意方向再关联拍摄项目")
        task.work_id = work_id
        task.status = models.PLAN_STATUS_LINKED
        self.db.flush()
        return task

    @transactional_method
    def delete(self, org_id: str, task_id: str) -> None:
        task = self.get(org_id, task_id)
        self.db.delete(task)
