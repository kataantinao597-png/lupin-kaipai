"""022：创意生成路由（从 app.py 拆分）。

- POST /api/ideas          生成 3 个创意方向
- POST /api/storyboard     生成结构化分镜
- POST /api/regenerate-shot  单镜头重生成（锁定保护）

路由层只做参数接收/校验/异常转换；业务逻辑在 services/。
"""
from __future__ import annotations

import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...ai_service import AIFatalError, AIConfigError, AIRetryableError, DEFAULT_MODEL
from ...ai.gateway import BudgetExceededError, TaskLimitExceededError
from ...auth.database import get_db
from ...auth.models import User
from ...core.deps import get_ai_gateway, get_request_model
from ...core.idempotency import (idempotency_begin, idempotency_invalidate)
from ...rbac import get_membership
from ...ai.prompts import get_prompt_version
from ...prompts import (
    build_idea_messages,
    build_regen_shot_messages,
    build_storyboard_messages,
    build_storyboard_v2_messages,
)
from ...schemas import Idea, Shot
from ...validator import validate_dict

router = APIRouter(tags=["ideas"])


# ---------- 请求模型（从 app.py 迁移） ----------

class ShootInput(BaseModel):
    device: str = "手机"
    people: int = 1
    lighting: str = ""
    location: str = ""
    target_duration_seconds: float = 60
    note: str = ""


class ProductInput(BaseModel):
    product_name: str = Field(..., min_length=1)
    description: str = ""
    selling_points: List[str] = Field(default_factory=list)
    price: str = ""
    audience: str = ""
    evidence_materials: str = ""
    shoot: ShootInput = Field(default_factory=ShootInput)


class IdeasRequest(BaseModel):
    product: ProductInput
    facts: List[dict] = Field(default_factory=list)
    # V3.0 B-4：可选产品档案。传了 product_id 后端自动把该产品的
    # 已确认事实并入 grounding；不传则行为与之前完全一致。
    product_id: Optional[str] = None
    org_id: Optional[str] = None  # 产品所属组织（不传则用默认组织）


class StoryboardRequest(BaseModel):
    product: ProductInput
    direction: str = Field(..., min_length=1)
    facts: List[dict] = Field(default_factory=list)
    # v2 新增：视频用途/可用资源/熟练度（均可选；传了 video_purpose 即走 v2 prompt）
    video_purpose: Optional[str] = None
    available_resources: List[str] = Field(default_factory=list)
    skill_level: Optional[str] = None
    # V3.0 B-4：可选产品档案（同 IdeasRequest）
    product_id: Optional[str] = None
    org_id: Optional[str] = None


VIDEO_PURPOSES = {"brand", "sales", "intro", "record"}


class RegenShotRequest(BaseModel):
    storyboard: dict
    shot_number: int = Field(..., ge=1)
    instruction: str = Field(..., min_length=1)
    locked: List[int] = Field(default_factory=list)


# ---------- V3.0 B-4：产品档案 grounding ----------

def _resolve_product_facts(req) -> tuple:
    """解析请求中的产品档案，返回 (product 摘要或 None, 合并后的 facts)。

    - 无 product_id：直接返回 (None, req.facts)，行为与之前完全一致。
    - 有 product_id：组织内查产品（跨组织 → 404），取其已确认事实
      （只看 confirmed+allow_use+未过期，未确认的一律不带入），
      与客户端 facts 按 id 去重合并。
    """
    if not req.product_id:
        return None, list(req.facts or [])
    from ...auth.database import get_session_factory
    from .ai_tasks import _resolve_org_id
    from .products import get_product_grounding, merge_grounding_facts
    db = get_session_factory()()
    try:
        org_id = _resolve_org_id(db, req.org_id)
        prod, product_facts = get_product_grounding(db, org_id, req.product_id)
        merged = merge_grounding_facts(req.facts, product_facts)
        return prod, merged
    finally:
        db.close()


# ---------- 校验函数 ----------

def _validate_ideas(data: dict):
    errors = []
    ideas_raw = data.get("ideas")
    if not isinstance(ideas_raw, list):
        return ["[结构] 缺少 ideas 数组"], None
    if len(ideas_raw) != 3:
        return [f"[规则] 创意方向必须为 3 个，实际 {len(ideas_raw)} 个"], None
    ideas = []
    for i, raw in enumerate(ideas_raw):
        try:
            ideas.append(Idea.model_validate(raw))
        except Exception as e:
            errors.append(f"[结构] ideas[{i}]: {e}")
    if errors:
        return errors, None
    return [], ideas


def _validate_shot(data: dict):
    try:
        shot = Shot.model_validate(data)
        return [], shot
    except Exception as e:
        errs = []
        for err in e.errors():
            loc = "/".join(str(p) for p in err.get("loc", ())) or "(root)"
            errs.append(f"[结构] {loc}: {err.get('msg', '')}")
        return errs, None


# ---------- 034：异步任务入口 ----------

def _optional_user(request: Request,
                   db: Session = Depends(get_db)) -> Optional[User]:
    """可选登录：合法 Bearer 返回用户，否则 None。

    同步 MVP 流程保持匿名可用；?async=true 分支要求登录（见下）。
    """
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else ""
    if not token:
        return None
    from ...auth.security import decode_access_token
    from ...repositories.user_repository import UserRepository
    user_id = decode_access_token(token)
    if not user_id:
        return None
    user = UserRepository(db).get_by_id(user_id)
    return user if user is not None and user.is_active else None


def _enqueue_ai_task(task_type: str, payload: dict, request: Request,
                     user: User, db: Session) -> dict:
    """034：同步接口的 ?async=true 入口——创建任务并返回 task_id。

    约束：异步任务只用服务端 AI 配置；请求头自带 Key（X-Lupin-Key）
    的不允许转异步（Key 不能落库），直接 400 提示走同步。

    V3.1 P0：强制登录；org_id 必传且调用者须为该组织成员
    （不存在/无权一律 404，防组织枚举）；created_by 取服务端登录用户。
    """
    if request.headers.get("X-Lupin-Key", "").strip():
        raise HTTPException(
            status_code=400,
            detail="自定义 Key 模式不支持异步任务（Key 不会持久化），"
                   "请去掉 async 参数使用同步接口")
    from ...ai_tasks.queue import TaskQueue
    from ...ai_tasks.repository import AITaskRepository
    from ...ai_tasks.runners import RUNNERS
    from ...auth.database import get_session_factory
    if task_type not in RUNNERS:
        raise HTTPException(status_code=400,
                            detail=f"任务类型 {task_type} 暂不支持异步")
    org_id = (payload or {}).get("org_id")
    ms = get_membership(db, user.id, org_id or "")
    if not org_id or ms is None:
        raise HTTPException(status_code=404, detail="组织不存在")
    org_id = ms.org_id
    session_factory = get_session_factory()
    repo = AITaskRepository(db)
    # S6.1：payload 注入 org_id，供 runner 在 gateway 调用前做事实引用检查
    payload = dict(payload or {})
    payload["_org_id"] = org_id
    # V2.1 #3：同 Idempotency-Key + 同请求体 → 直接返回已有任务，不重复建任务
    idem = idempotency_begin("enqueue:" + task_type, request, payload)
    if idem.replay is not None:
        old_task = repo.get((idem.replay or {}).get("task_id") or "",
                            org_id)
        if old_task is not None:
            return {"task_id": old_task.id, "status": old_task.status,
                    "query_url": f"/api/v1/ai-tasks/{old_task.id}",
                    "idempotent_replay": True}
        # 任务记录已不在：清掉失效的幂等记录，重新建任务（不静默返回死链接）
        idempotency_invalidate(
            "enqueue:" + task_type,
            request.headers.get("Idempotency-Key", ""))
        idem = idempotency_begin("enqueue:" + task_type, request, payload)
    try:
        # 137：同步接口转异步（用户在等待结果）→ high 优先级
        task = repo.create(org_id=org_id, task_type=task_type,
                           payload=payload, created_by=user.id,
                           priority="high")
        task_id = task.id
    except Exception:
        idem.abort()
        raise
    idem.finish({"task_id": task_id})
    runner = RUNNERS[task_type]

    def _runner(tid: str, is_cancelled):
        return runner(tid, is_cancelled, dict(payload))

    TaskQueue(session_factory).submit(task_id, _runner, priority="high")
    return {"task_id": task_id, "status": "pending",
            "query_url": f"/api/v1/ai-tasks/{task_id}"}


# ---------- 路由 ----------

@router.post("/api/ideas")
def create_ideas(
    req: IdeasRequest,
    request: Request,
    gw=Depends(get_ai_gateway),
    async_mode: bool = Query(default=False, alias="async"),
    user: Optional[User] = Depends(_optional_user),
    db: Session = Depends(get_db),
):
    # 034：?async=true 走异步任务模式（V3.1 P0：异步建任务强制登录）
    if async_mode:
        if user is None:
            raise HTTPException(status_code=401, detail="异步任务需要登录")
        return _enqueue_ai_task("ideas", {
            "product": req.product.model_dump(),
            "facts": req.facts,
            # V3.0 B-4：异步 runner 在执行时解析产品事实
            "product_id": req.product_id,
            "org_id": req.org_id,
        }, request, user, db)
    _model = get_request_model(request)
    # V2.1 #3：Idempotency-Key 去重——同 key 同体直接回放，不重复调模型扣费
    idem = idempotency_begin("ideas", request, req.model_dump(),
                             scope=_model or "")
    if idem.replay is not None:
        return idem.replay
    try:
        product = req.product.model_dump()
        # V3.0 B-4：产品档案的已确认事实自动并入 grounding
        prod_ref, facts = _resolve_product_facts(req)

        def make_messages(feedback: Optional[str]):
            msgs = build_idea_messages(product, facts)
            if feedback:
                msgs.append({"role": "user", "content": feedback})
            return msgs

        try:
            ideas, meta = gw.generate_with_retry(
                "ideas", make_messages, _validate_ideas,
                model=_model or None, temperature=0.7,
                prompt_version=get_prompt_version("ideas"))
        except BudgetExceededError as e:
            raise HTTPException(status_code=402, detail=str(e))
        except TaskLimitExceededError as e:
            raise HTTPException(status_code=429, detail=str(e))
        except (AIFatalError, AIRetryableError, AIConfigError) as e:
            raise HTTPException(status_code=502, detail=str(e))
        resp = {
            "ideas": [i.model_dump() for i in ideas],
            "attempts": meta["attempts"],
            "usage": meta["usage"],
        }
        if prod_ref is not None:
            resp["product_ref"] = prod_ref
            resp["product_facts_used"] = [
                f.get("id") for f in facts if f.get("id")]
    except Exception:
        idem.abort()
        raise
    return idem.finish(resp)


@router.post("/api/storyboard")
def create_storyboard(
    req: StoryboardRequest,
    request: Request,
    gw=Depends(get_ai_gateway),
    async_mode: bool = Query(default=False, alias="async"),
    user: Optional[User] = Depends(_optional_user),
    db: Session = Depends(get_db),
):
    # 034：?async=true 走异步任务模式（V3.1 P0：异步建任务强制登录）
    if async_mode:
        if user is None:
            raise HTTPException(status_code=401, detail="异步任务需要登录")
        return _enqueue_ai_task("storyboard", {
            "product": req.product.model_dump(),
            "direction": req.direction,
            "facts": req.facts,
            "video_purpose": req.video_purpose,
            "available_resources": req.available_resources,
            "skill_level": req.skill_level,
            # V3.0 B-4：异步 runner 在执行时解析产品事实
            "product_id": req.product_id,
            "org_id": req.org_id,
        }, request, user, db)
    _model = get_request_model(request)
    product = req.product.model_dump()

    # V2.1 #3：Idempotency-Key 去重——同 key 同体直接回放，不重复调模型扣费
    idem = idempotency_begin("storyboard", request, req.model_dump(),
                             scope=_model or "")
    if idem.replay is not None:
        return idem.replay

    # V3.0 B-4：产品档案的已确认事实自动并入 grounding
    prod_ref, facts = _resolve_product_facts(req)

    # v2 分流：前端传了合法 video_purpose 即用 storyboard_v2（含验收标准/时长拆分等）
    use_v2 = (req.video_purpose or "").strip() in VIDEO_PURPOSES
    v2_purpose = req.video_purpose.strip() if use_v2 else "intro"

    def make_messages(feedback: Optional[str]):
        if use_v2:
            return build_storyboard_v2_messages(
                product, req.direction, v2_purpose, req.available_resources,
                facts, feedback)
        return build_storyboard_messages(product, req.direction, facts, feedback)

    try:
        try:
            sb, meta = gw.generate_with_retry(
                "storyboard", make_messages, validate_dict,
                model=_model or None, temperature=0.3,
                prompt_version=get_prompt_version("storyboard_v2" if use_v2 else "storyboard"))
        except BudgetExceededError as e:
            raise HTTPException(status_code=402, detail=str(e))
        except TaskLimitExceededError as e:
            raise HTTPException(status_code=429, detail=str(e))
        except (AIFatalError, AIRetryableError, AIConfigError) as e:
            raise HTTPException(status_code=502, detail=str(e))
        resp = {
            "storyboard": sb.model_dump(),
            "attempts": meta["attempts"],
            "usage": meta["usage"],
        }
        if prod_ref is not None:
            resp["product_ref"] = prod_ref
            resp["product_facts_used"] = [
                f.get("id") for f in facts if f.get("id")]
    except Exception:
        idem.abort()
        raise
    return idem.finish(resp)


@router.post("/api/regenerate-shot")
def regenerate_shot(
    req: RegenShotRequest,
    request: Request,
    gw=Depends(get_ai_gateway),
):
    _model = get_request_model(request)
    # V2.1 #3：Idempotency-Key 去重——同 key 同体直接回放，不重复调模型扣费
    idem = idempotency_begin("regenerate-shot", request, req.model_dump(),
                             scope=_model or "")
    if idem.replay is not None:
        return idem.replay
    sb = req.storyboard
    shots = sb.get("shots", [])
    if not any(s.get("shot_number") == req.shot_number for s in shots):
        idem.abort()
        raise HTTPException(status_code=400, detail=f"镜头 {req.shot_number} 不存在")
    if req.shot_number in req.locked:
        idem.abort()
        raise HTTPException(status_code=400, detail=f"镜头 {req.shot_number} 已锁定，不能重生成")

    def make_messages(feedback: Optional[str]):
        return build_regen_shot_messages(sb, req.shot_number, req.instruction, req.locked, feedback)

    try:
        try:
            new_shot, meta = gw.generate_with_retry(
                "regen_shot", make_messages, _validate_shot,
                model=_model or None, temperature=0.4,
                prompt_version=get_prompt_version("regen_shot"))
        except BudgetExceededError as e:
            raise HTTPException(status_code=402, detail=str(e))
        except TaskLimitExceededError as e:
            raise HTTPException(status_code=429, detail=str(e))
        except (AIFatalError, AIRetryableError, AIConfigError) as e:
            raise HTTPException(status_code=502, detail=str(e))

        if new_shot.shot_number != req.shot_number:
            raise HTTPException(status_code=502, detail="模型返回了错误的镜头序号，已丢弃该结果")

        new_sb = json.loads(json.dumps(sb))  # 深拷贝
        for i, s in enumerate(new_sb["shots"]):
            if s.get("shot_number") == req.shot_number:
                new_sb["shots"][i] = new_shot.model_dump()
                break
        # 锁定镜头保护：确认 locked 镜头内容未被改动
        for s_old, s_new in zip(sb["shots"], new_sb["shots"]):
            if s_old.get("shot_number") in req.locked and s_old != s_new:
                raise HTTPException(status_code=502, detail=f"锁定镜头 {s_old.get('shot_number')} 被意外改动，已中止")

        errors, _ = validate_dict(new_sb)
        warning = "; ".join(errors) if errors else ""
        resp = {"shot": new_shot.model_dump(), "storyboard": new_sb,
                "warning": warning, "attempts": meta["attempts"]}
    except Exception:
        idem.abort()
        raise
    return idem.finish(resp)
