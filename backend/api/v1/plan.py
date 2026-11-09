"""V3.0 C：内容策划工作台 API。

从营销目标到创意方向的策划流程：
  创建策划任务 → AI 生成 3-5 个候选创意 → 用户选择 → 可行性检查 →
  关联 V2.1 拍摄项目 → 继续走既有分镜/实拍流程。

铁律：
- 不重复生成完整的拍摄执行单；分镜/实拍一律走 V2.1 既有接口。
- 事实只取 confirmed+allow_use（复用 B-4 get_product_grounding）。
- 实拍条件不足时走纯规则可行性检查（feasibility_v2），不生成
  不可执行的创意还宣称可直接拍摄。
- 无 AI key 时 503 诚实失败，不拿预制文本冒充。
"""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ... import models
from ...ai.prompts import build_plan_messages, get_prompt_version
from ...ai_service import AIFatalError, AIConfigError, AIRetryableError
from ...ai.gateway import BudgetExceededError, TaskLimitExceededError
from ...ai.secure_messages import _secure_messages
from ...auth.database import get_db
from ...auth.deps import get_current_user
from ...auth.models import User
from ...core import error_codes
from ...core.deps import get_ai_gateway, get_request_model
from ...core.exceptions import BusinessError
from ...core.idempotency import idempotency_begin
from ...core.response import success
from ...rbac import get_membership, has_permission, assert_can
from ..v1.products import get_product_grounding
from ...repositories.plan_repository import (
    ContentTaskNotFoundError,
    PlanRepository,
)
from ...repositories.product_repository import ProductRepository
from ...repositories.work_repository import WorkRepository

router = APIRouter(prefix="/plan", tags=["v1-plan"])

GOALS = models.PLAN_GOALS

# 人工复核提示：策划结果固定携带
REVIEW_REMINDER = (
    "候选创意由 AI 生成，仅供策划参考。实拍前请人工确认创意与产品事实、"
    "宣传限制的一致性，本工具不保证广告合规。"
)


# ---------- 请求/响应模型 ----------

class PlanTaskCreate(BaseModel):
    title: str = Field(default="", max_length=256)
    content_goal: str = Field(default="brand", max_length=16)
    product_id: Optional[str] = Field(default=None, max_length=32)
    audience: str = Field(default="", max_length=256)
    content_purpose: str = Field(default="", max_length=256)
    core_message: str = Field(default="", max_length=5000)
    selling_points: list[str] = Field(default_factory=list, max_length=50)
    style: str = Field(default="", max_length=128)
    target_duration_seconds: int = Field(default=30, ge=5, le=3600)
    target_platforms: list[str] = Field(default_factory=list, max_length=10)
    shoot_conditions: dict = Field(default_factory=dict)
    ad_restrictions: str = Field(default="", max_length=5000)


class PlanTaskUpdate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=256)
    content_goal: Optional[str] = Field(default=None, max_length=16)
    product_id: Optional[str] = Field(default=None, max_length=32)
    audience: Optional[str] = Field(default=None, max_length=256)
    content_purpose: Optional[str] = Field(default=None, max_length=256)
    core_message: Optional[str] = Field(default=None, max_length=5000)
    selling_points: Optional[list[str]] = Field(default=None, max_length=50)
    style: Optional[str] = Field(default=None, max_length=128)
    target_duration_seconds: Optional[int] = Field(default=None, ge=5, le=3600)
    target_platforms: Optional[list[str]] = Field(default=None, max_length=10)
    shoot_conditions: Optional[dict] = None
    ad_restrictions: Optional[str] = Field(default=None, max_length=5000)
    status: Optional[str] = Field(default=None, max_length=16)


class PlanIdea(BaseModel):
    """单个候选创意方向（任务书 §5：每个方向说明的 6 要素）。"""
    name: str = ""
    goal: str = ""
    core_expression: str = ""
    hook_opening: str = ""
    structure: str = ""
    suggested_shots: str = ""
    difficulty: str = ""
    shoot_notes: str = ""
    fact_refs: list[str] = Field(default_factory=list)


class PlanSelectRequest(BaseModel):
    index: int = Field(..., ge=0)


class PlanLinkWorkRequest(BaseModel):
    work_id: Optional[str] = Field(default=None, max_length=32)
    create_new: bool = False
    title: str = Field(default="", max_length=256)


# ---------- 内部 helper ----------

def _require_plan_perm(db: Session, user: User, org_id: str,
                       action: str, what: str):
    """策划权限；无权一律 404（防枚举，与 products.py 一致）。"""
    if not org_id:
        raise BusinessError("必须指定 org_id",
                            code=error_codes.INVALID_PARAM, http_status=400)
    ms = get_membership(db, user.id, org_id)
    if ms is None or not has_permission(ms.role, action):
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    assert_can(ms.role, action, what)
    return ms


def _task_or_404(repo: PlanRepository, org_id: str, task_id: str):
    try:
        return repo.get(org_id, task_id)
    except ContentTaskNotFoundError:
        raise BusinessError("策划任务不存在",
                            code=error_codes.PLAN_TASK_NOT_FOUND,
                            http_status=404)


def _task_out(task: models.ContentTask) -> dict:
    return {
        "id": task.id, "org_id": task.org_id,
        "title": task.title or "", "content_goal": task.content_goal,
        "product_id": task.product_id,
        "audience": task.audience or "",
        "content_purpose": task.content_purpose or "",
        "core_message": task.core_message or "",
        "selling_points": task.selling_points or [],
        "style": task.style or "",
        "target_duration_seconds": task.target_duration_seconds or 30,
        "target_platforms": task.target_platforms or [],
        "shoot_conditions": task.shoot_conditions or {},
        "ad_restrictions": task.ad_restrictions or "",
        "candidates": task.candidates or [],
        "selected_index": task.selected_index,
        "selected_idea": task.selected_idea,
        "work_id": task.work_id,
        "status": task.status,
        "ai_meta": task.ai_meta or {},
        "created_by": task.created_by or "",
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }


def _validate_plan(data: dict):
    """校验模型返回的候选创意结构：3-5 个，每项 6 要素齐全。"""
    errors = []
    raw = data.get("candidates")
    if not isinstance(raw, list):
        return ["[结构] 缺少 candidates 数组"], None
    if not (3 <= len(raw) <= 5):
        return [f"[规则] 候选创意必须为 3-5 个，实际 {len(raw)} 个"], None
    ideas = []
    for i, item in enumerate(raw):
        try:
            ideas.append(PlanIdea.model_validate(item))
        except Exception as e:
            errors.append(f"[结构] candidates[{i}]: {e}")
    if errors:
        return errors, None
    return [], ideas


def _plan_prompt_params(task: models.ContentTask, product: dict | None) -> dict:
    """组装 prompt 参数。"""
    prod = product or {}
    return {
        "product": {
            "product_name": prod.get("name") or task.title or "（未命名产品）",
            "description": task.core_message or "",
            "selling_points": task.selling_points or [],
            "price": "",
            "audience": task.audience or "",
            "evidence_materials": "",
            "shoot": task.shoot_conditions or {},
        },
        "content_goal": task.content_goal,
        "audience": task.audience or "",
        "content_purpose": task.content_purpose or "",
        "core_message": task.core_message or "",
        "selling_points": task.selling_points or [],
        "style": task.style or "",
        "target_duration_seconds": task.target_duration_seconds or 30,
        "target_platforms": task.target_platforms or [],
        "shoot": task.shoot_conditions or {},
        "ad_restrictions": task.ad_restrictions or "",
    }


# ---------- CRUD ----------

@router.post("/tasks", status_code=status.HTTP_201_CREATED)
def create_task(req: PlanTaskCreate,
                org_id: str = Query(...),
                user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    _require_plan_perm(db, user, org_id, "plan:create", "创建策划任务")
    if req.content_goal not in GOALS:
        raise BusinessError(f"不支持的内容目标: {req.content_goal}",
                            code=error_codes.INVALID_PARAM, http_status=400)
    product_id = None
    if req.product_id:
        prod, _ = get_product_grounding(db, org_id, req.product_id)
        product_id = prod["id"]
    repo = PlanRepository(db)
    try:
        task = repo.create(
            org_id, created_by=user.id,
            title=req.title, content_goal=req.content_goal,
            product_id=product_id, audience=req.audience,
            content_purpose=req.content_purpose,
            core_message=req.core_message,
            selling_points=req.selling_points, style=req.style,
            target_duration_seconds=req.target_duration_seconds,
            target_platforms=req.target_platforms,
            shoot_conditions=req.shoot_conditions,
            ad_restrictions=req.ad_restrictions)
    except ValueError as e:
        raise BusinessError(str(e), code=error_codes.INVALID_PARAM,
                            http_status=400)
    return success(_task_out(task))


@router.get("/tasks")
def list_tasks(org_id: str = Query(...),
               goal: Optional[str] = Query(default=None),
               user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    _require_plan_perm(db, user, org_id, "plan:read", "查看策划任务")
    repo = PlanRepository(db)
    tasks = repo.list_by_org(org_id, goal=goal)
    return success([_task_out(t) for t in tasks])


@router.get("/tasks/{task_id}")
def get_task(task_id: str,
             org_id: str = Query(...),
             user: User = Depends(get_current_user),
             db: Session = Depends(get_db)):
    _require_plan_perm(db, user, org_id, "plan:read", "查看策划任务")
    repo = PlanRepository(db)
    return success(_task_out(_task_or_404(repo, org_id, task_id)))


@router.patch("/tasks/{task_id}")
def update_task(task_id: str, req: PlanTaskUpdate,
                org_id: str = Query(...),
                user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    _require_plan_perm(db, user, org_id, "plan:update", "修改策划任务")
    repo = PlanRepository(db)
    fields = req.model_dump(exclude_none=True)
    if "product_id" in fields and fields["product_id"]:
        prod, _ = get_product_grounding(db, org_id, fields["product_id"])
        fields["product_id"] = prod["id"]
    try:
        task = repo.update_settings(org_id, task_id, fields)
    except ValueError as e:
        raise BusinessError(str(e), code=error_codes.INVALID_PARAM,
                            http_status=400)
    return success(_task_out(task))


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(task_id: str,
                org_id: str = Query(...),
                user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    _require_plan_perm(db, user, org_id, "plan:delete", "删除策划任务")
    repo = PlanRepository(db)
    repo.delete(org_id, task_id)
    return None


# ---------- 生成候选创意 ----------

@router.post("/tasks/{task_id}/generate")
def generate_candidates(task_id: str,
                        org_id: str = Query(...),
                        request: Request = None,
                        user: User = Depends(get_current_user),
                        db: Session = Depends(get_db),
                        gw=Depends(get_ai_gateway)):
    _require_plan_perm(db, user, org_id, "plan:update", "生成候选创意")
    repo = PlanRepository(db)
    task = _task_or_404(repo, org_id, task_id)

    _model = get_request_model(request)
    idem = idempotency_begin("content_plan", request,
                             {"task_id": task_id, "org_id": org_id,
                              "goal": task.content_goal,
                              "updated_at": task.updated_at.isoformat()
                              if task.updated_at else ""},
                             scope=_model or "", user_id=user.id)
    if idem.replay is not None:
        return idem.replay
    try:
        # B-4 grounding：只取 confirmed+allow_use 的事实
        product, facts = None, []
        if task.product_id:
            try:
                product, facts = get_product_grounding(
                    db, org_id, task.product_id)
            except BusinessError:
                # 产品档案被删 → 不带 grounding 继续（SET NULL 语义）
                product, facts = None, []
        params = _plan_prompt_params(task, product)

        def make_messages(feedback: Optional[str]):
            msgs = build_plan_messages(params, facts)
            if feedback:
                msgs.append({"role": "user", "content": feedback})
            # S-3 同类闭合：与 runner 同款隔离
            return _secure_messages(msgs)

        try:
            ideas, meta = gw.generate_with_retry(
                "content_plan", make_messages, _validate_plan,
                model=_model or None, temperature=0.7,
                prompt_version=get_prompt_version("content_plan"))
        except BudgetExceededError as e:
            raise HTTPException(status_code=402, detail=str(e))
        except TaskLimitExceededError as e:
            raise HTTPException(status_code=429, detail=str(e))
        except (AIFatalError, AIRetryableError, AIConfigError) as e:
            # 无 key / 模型失败 → 503 诚实失败，不存草稿
            raise HTTPException(
                status_code=503,
                detail=f"AI 策划服务暂不可用（{e}），请稍后再试")

        cands = [i.model_dump() for i in ideas]
        task = repo.save_candidates(
            org_id, task_id, cands,
            ai_meta={"attempts": meta.get("attempts"),
                     "usage": meta.get("usage"),
                     "prompt_version": get_prompt_version("content_plan"),
                     "product_facts_used": [f.get("id") for f in facts
                                            if f.get("id")]})
        resp = success(_task_out(task))
        resp["review_reminder"] = REVIEW_REMINDER
    except Exception:
        idem.abort()
        raise
    return idem.finish(resp)


# ---------- 选择创意 ----------

@router.post("/tasks/{task_id}/select")
def select_candidate(task_id: str, req: PlanSelectRequest,
                     org_id: str = Query(...),
                     user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    _require_plan_perm(db, user, org_id, "plan:update", "选择创意方向")
    repo = PlanRepository(db)
    try:
        task = repo.select_idea(org_id, task_id, req.index)
    except ValueError as e:
        raise BusinessError(str(e), code=error_codes.INVALID_PARAM,
                            http_status=400)
    return success(_task_out(task))


# ---------- 可行性检查（纯规则，不调模型） ----------

@router.post("/tasks/{task_id}/feasibility")
def check_task_feasibility(task_id: str,
                           org_id: str = Query(...),
                           user: User = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    """对选中创意做纯规则可行性检查。

    实拍条件不足时返回 need_confirm/need_alternative（一定带替代方案），
    不生成不可执行的创意还宣称可直接拍摄。
    """
    _require_plan_perm(db, user, org_id, "plan:read", "可行性检查")
    repo = PlanRepository(db)
    task = _task_or_404(repo, org_id, task_id)
    if task.selected_idea is None:
        raise BusinessError("请先选择一个创意方向再做可行性检查",
                            code=error_codes.PLAN_IDEA_NOT_SELECTED,
                            http_status=400)
    from ...feasibility_v2 import check_feasibility
    from ...ai.prompts.plan_v1 import GOAL_LABELS
    product = {"product_name": task.title or "（未命名产品）",
               "description": task.core_message or "",
               "selling_points": task.selling_points or []}
    if task.product_id:
        try:
            prod_repo = ProductRepository(db)
            p = prod_repo.get(task.product_id, org_id)
            if p is not None:
                product["product_name"] = p.name or product["product_name"]
        except Exception:
            pass
    result = check_feasibility(
        product, task.shoot_conditions or {},
        GOAL_LABELS.get(task.content_goal, "品牌宣传"))
    result["review_reminder"] = REVIEW_REMINDER
    return success(result)


# ---------- 关联 V2.1 拍摄项目 ----------

@router.post("/tasks/{task_id}/link-work")
def link_work(task_id: str, req: PlanLinkWorkRequest,
              org_id: str = Query(...),
              user: User = Depends(get_current_user),
              db: Session = Depends(get_db)):
    """把选中的创意关联到 V2.1 拍摄项目。

    - 传 work_id：关联已有项目，并把选中创意的方向写入该 work 的
      data_json.direction（走 WorkRepository 既有更新逻辑，不重实现）。
    - create_new=true：新建拍摄项目（标题/产品/direction 预填），
      后续继续走既有分镜+实拍流程。
    """
    _require_plan_perm(db, user, org_id, "plan:update", "关联拍摄项目")
    repo = PlanRepository(db)
    task = _task_or_404(repo, org_id, task_id)
    if task.selected_idea is None:
        raise BusinessError("请先选择一个创意方向再关联拍摄项目",
                            code=error_codes.PLAN_IDEA_NOT_SELECTED,
                            http_status=400)
    idea = task.selected_idea or {}
    direction = (idea.get("name") or "") + "：" + (idea.get("core_expression") or "")
    work_repo = WorkRepository(db)

    if req.create_new:
        _require_plan_perm(db, user, org_id, "work:create", "创建拍摄项目")
        data = {
            "direction": direction,
            "plan_task_id": task.id,
            "plan_idea": idea,
        }
        if task.product_id:
            try:
                prod_repo = ProductRepository(db)
                p = prod_repo.get(task.product_id, org_id)
                if p is not None:
                    data["product"] = {
                        "product_name": p.name or "",
                        "description": p.brand_profile or "",
                        "selling_points": p.selling_points or [],
                    }
            except Exception:
                pass
        work = work_repo.create(
            org_id, user.id,
            title=(req.title or task.title or "未命名项目").strip(),
            data=data, status="draft")
        work_id = work.id
    else:
        if not req.work_id:
            raise BusinessError("请指定 work_id 或 create_new=true",
                                code=error_codes.INVALID_PARAM,
                                http_status=400)
        work = work_repo.get(req.work_id, org_id)
        if work is None:
            raise BusinessError("拍摄项目不存在",
                                code=error_codes.WORK_NOT_FOUND,
                                http_status=404)
        # 读出现有 data_json，只追加 direction/plan 引用，不改写其他内容
        try:
            data = json.loads(work.data_json or "{}")
        except Exception:
            data = {}
        data["direction"] = direction
        data["plan_task_id"] = task.id
        data["plan_idea"] = idea
        work_repo.update(req.work_id, org_id, data=data)
        work_id = req.work_id

    task = repo.link_work(org_id, task_id, work_id)
    out = _task_out(task)
    out["linked_work_id"] = work_id
    return success(out)
