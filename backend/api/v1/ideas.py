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

from ...ai_service import AIFatalError, AIConfigError, AIRetryableError, DEFAULT_MODEL
from ...ai.gateway import BudgetExceededError, TaskLimitExceededError
from ...core.deps import get_ai_gateway, get_request_model
from ...core.idempotency import (idempotency_begin, idempotency_invalidate)
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


class StoryboardRequest(BaseModel):
    product: ProductInput
    direction: str = Field(..., min_length=1)
    facts: List[dict] = Field(default_factory=list)
    # v2 新增：视频用途/可用资源/熟练度（均可选；传了 video_purpose 即走 v2 prompt）
    video_purpose: Optional[str] = None
    available_resources: List[str] = Field(default_factory=list)
    skill_level: Optional[str] = None


VIDEO_PURPOSES = {"brand", "sales", "intro", "record"}


class RegenShotRequest(BaseModel):
    storyboard: dict
    shot_number: int = Field(..., ge=1)
    instruction: str = Field(..., min_length=1)
    locked: List[int] = Field(default_factory=list)


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

def _enqueue_ai_task(task_type: str, payload: dict, request: Request) -> dict:
    """034：同步接口的 ?async=true 入口——创建任务并返回 task_id。

    约束：异步任务只用服务端 AI 配置；请求头自带 Key（X-Lupin-Key）
    的不允许转异步（Key 不能落库），直接 400 提示走同步。
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
    from .ai_tasks import _resolve_org_id
    if task_type not in RUNNERS:
        raise HTTPException(status_code=400,
                            detail=f"任务类型 {task_type} 暂不支持异步")
    session_factory = get_session_factory()
    db = session_factory()
    try:
        org_id = _resolve_org_id(db, None)
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
                               payload=payload, priority="high")
            task_id = task.id
        except Exception:
            idem.abort()
            raise
        idem.finish({"task_id": task_id})
    finally:
        db.close()
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
):
    # 034：?async=true 走异步任务模式
    if async_mode:
        return _enqueue_ai_task("ideas", {
            "product": req.product.model_dump(),
            "facts": req.facts,
        }, request)
    _model = get_request_model(request)
    # V2.1 #3：Idempotency-Key 去重——同 key 同体直接回放，不重复调模型扣费
    idem = idempotency_begin("ideas", request, req.model_dump(),
                             scope=_model or "")
    if idem.replay is not None:
        return idem.replay
    try:
        product = req.product.model_dump()

        def make_messages(feedback: Optional[str]):
            msgs = build_idea_messages(product, req.facts)
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
):
    # 034：?async=true 走异步任务模式
    if async_mode:
        return _enqueue_ai_task("storyboard", {
            "product": req.product.model_dump(),
            "direction": req.direction,
            "facts": req.facts,
            "video_purpose": req.video_purpose,
            "available_resources": req.available_resources,
            "skill_level": req.skill_level,
        }, request)
    _model = get_request_model(request)
    product = req.product.model_dump()

    # V2.1 #3：Idempotency-Key 去重——同 key 同体直接回放，不重复调模型扣费
    idem = idempotency_begin("storyboard", request, req.model_dump(),
                             scope=_model or "")
    if idem.replay is not None:
        return idem.replay

    # v2 分流：前端传了合法 video_purpose 即用 storyboard_v2（含验收标准/时长拆分等）
    use_v2 = (req.video_purpose or "").strip() in VIDEO_PURPOSES
    v2_purpose = req.video_purpose.strip() if use_v2 else "intro"

    def make_messages(feedback: Optional[str]):
        if use_v2:
            return build_storyboard_v2_messages(
                product, req.direction, v2_purpose, req.available_resources,
                req.facts, feedback)
        return build_storyboard_messages(product, req.direction, req.facts, feedback)

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
