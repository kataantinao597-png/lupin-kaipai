"""鲁品开拍 - FastAPI 后端入口（022 重构后）。

本文件只保留：
- app 创建与中间件
- 路由挂载
- 启动初始化（AI 单例、数据库建表）
- /api/health 与 / 静态首页

业务路由已拆分到 backend/api/v1/：
- ideas.py       /api/ideas, /api/storyboard, /api/regenerate-shot
- risks.py       /api/risk-check, /api/delivery-check
- exports.py     /api/export, /api/shooting-checklist, /api/validate, /api/export-srt
- feasibility.py /api/feasibility-check
- works.py, orgs.py（Phase 2 新体系，/api/v1 前缀）

业务逻辑在 backend/services/；依赖注入在 backend/core/deps.py。
"""
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from .ai_service import DEFAULT_MODEL
from .auth.router import router as auth_router
from .core.config import get_settings
from .core.logging import get_logger
from .core.request_id import RequestIDMiddleware, get_request_id
from .core.metrics import MetricsMiddleware
from .security.headers import SecurityHeadersMiddleware  # Phase 9-1
from .security.rate_limit import RateLimitMiddleware  # Phase 9-3
from .core.response import error as error_envelope
from .core import error_codes
from .core.exceptions import AppError, LupinError

log = get_logger("app")

import time as _time
_START_TIME = _time.time()  # S9.3：uptime 计算基准

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

app = FastAPI(title="鲁品开拍", version="0.1.0")
# 注意：不在此处 include_router（FastAPI 0.143.2 bug），统一由下方 _mount() 挂载
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO(066)：生产收紧为明确来源
    allow_methods=["*"],
    allow_headers=["*"],
)
# 026/027：请求 ID 中间件（响应头 X-Request-ID，错误信封内 request_id 同值）
app.add_middleware(RequestIDMiddleware)
# S9.3：请求指标中间件（挂在 RequestID 内侧，保证 request_id 已就绪）
app.add_middleware(MetricsMiddleware)
# Phase 9-3：分级限流（auth 5/min/IP、AI 20/min/用户、默认 100/min/IP）
app.add_middleware(RateLimitMiddleware)
# Phase 9-1：安全响应头（CSP report-only，不拦截）
app.add_middleware(SecurityHeadersMiddleware)


def _is_v1_path(request) -> bool:
    """新 /api/v1/* 接口走标准信封；老 /api/* 保持原有响应格式（兼容）。"""
    return request.url.path.startswith("/api/v1")


def _code_for_http_status(status_code: int) -> int:
    """HTTP 状态码 → 兜底业务码（v1 路径下 HTTPException 转换用）。"""
    return {
        400: error_codes.INVALID_PARAM,
        401: error_codes.UNAUTHENTICATED,
        403: error_codes.PERMISSION_DENIED,
        404: error_codes.RESOURCE_NOT_FOUND,
        409: error_codes.VERSION_CONFLICT,
        422: error_codes.INVALID_PARAM,
        429: error_codes.AI_RATE_LIMITED,
        503: error_codes.SERVICE_UNAVAILABLE,
    }.get(status_code, error_codes.SYSTEM_ERROR)


# ---------- 026：全局异常处理 ----------
# TODO(026)：老 /api/* 接口（ideas/risks/exports/feasibility.py）仍抛 HTTPException，
# 其错误响应保持 {"detail": ...} 原格式；迁移到 v2 时再统一为标准信封。

@app.exception_handler(AppError)
async def _app_error_handler(request, exc: AppError):
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=exc.http_status,
        content=error_envelope(exc.code, exc.message,
                               request_id=get_request_id() or None,
                               details=exc.details),
    )


@app.exception_handler(LupinError)
async def _lupin_error_handler(request, exc: LupinError):
    """021 兼容：services 层字符串码异常同样转为标准信封。"""
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=exc.status_code,
        content=error_envelope(exc.code, exc.message,
                               request_id=get_request_id() or None),
    )


@app.exception_handler(HTTPException)
async def _http_exception_handler(request, exc: HTTPException):
    """HTTPException：v1 路径转标准信封并映射业务码；老路径保持原格式。"""
    from fastapi.responses import JSONResponse
    if _is_v1_path(request):
        return JSONResponse(
            status_code=exc.status_code,
            content=error_envelope(
                _code_for_http_status(exc.status_code),
                str(exc.detail),
                request_id=get_request_id() or None),
        )
    # 老接口：保持原格式
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": exc.detail},
                        headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def _validation_handler(request, exc: RequestValidationError):
    """Pydantic 请求校验失败：v1 转标准信封；老路径保持原格式。"""
    from fastapi.responses import JSONResponse
    if _is_v1_path(request):
        return JSONResponse(
            status_code=422,
            content=error_envelope(
                error_codes.INVALID_PARAM, "请求参数非法",
                request_id=get_request_id() or None,
                details=exc.errors()),
        )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.exception_handler(Exception)
async def _unhandled_handler(request, exc: Exception):
    from fastapi.responses import JSONResponse
    log.error(f"未处理异常 request_id={get_request_id()}: {type(exc).__name__}: {exc}")
    if _is_v1_path(request):
        return JSONResponse(
            status_code=500,
            content=error_envelope(error_codes.SYSTEM_ERROR, "系统内部错误",
                                   request_id=get_request_id() or None),
        )
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


@app.on_event("startup")
def _startup():
    # 024：启动时验证配置，缺失给明确错误/警告
    settings = get_settings()
    for w in settings.validate_startup():
        log.info(w)
        print(f"[启动警告] {w}")

    # AI 单例预热（无 Key 时跳过，接口返回 503）
    try:
        from .core import deps as _deps
        from .ai_service import AIService
        if settings.LUPIN_API_KEY:
            _deps.get_ai_service._singleton = AIService(
                api_key=settings.LUPIN_API_KEY,
                base_url=settings.LUPIN_API_BASE,
            )
            log.info("AI 服务已初始化")
    except Exception as e:  # noqa: BLE001
        log.info(f"AI 服务未初始化: {e}")

    # Phase 2：组织/权限表与认证组共用 backend/data/lupin.db
    try:
        from . import models  # noqa: F401
        from .auth.database import get_engine
        get_engine()
    except Exception as e:  # noqa: BLE001
        log.info(f"权限体系数据库初始化失败: {e}")

    # S6.3：注册领域事件默认订阅者（分镜/事实变更 → 审核自动失效）
    try:
        from .core.events import get_event_bus, register_default_subscribers
        from .auth.database import get_session_factory
        register_default_subscribers(get_event_bus(), get_session_factory())
        log.info("领域事件订阅者已注册")
    except Exception as e:  # noqa: BLE001
        log.info(f"领域事件订阅者注册失败: {e}")

    # S8.2：启动媒体处理工作线程（失败不阻断启动）
    try:
        from .media.worker import start_media_worker
        start_media_worker()
        log.info("媒体处理线程已启动")
    except Exception as e:  # noqa: BLE001
        log.info(f"媒体处理线程启动失败: {e}")


# ---------- 业务路由挂载（022） ----------
# 注意：FastAPI 0.143.2 的 include_router 有 bug（只挂载部分路由），
# 此处用手动 extend 绕过，保证 API 行为与重构前一致。

def _mount(router, prefix: str = ""):
    """手动挂载 APIRouter 的所有路由（绕过 include_router bug）。

    prefix 非空时，给每个路由路径加前缀（用于 /api/v1 下的新体系路由）。
    注意：Starlette 用预编译的 path_regex 做路径匹配，只改 path/path_format
    不够，必须重新编译（026 组修复：此前 v1 路由 openapi 可见但请求 404）。
    """
    import copy
    from starlette.routing import compile_path
    from fastapi.routing import request_response  # 必须用 FastAPI 版（含 astack 初始化），不用 starlette 版
    for route in router.routes:
        if prefix:
            nr = copy.copy(route)
            nr.path = prefix + route.path
            # 同步 path_format（Starlette 内部用）并重新编译匹配正则
            if hasattr(nr, "path_format"):
                nr.path_format = prefix + route.path_format
                nr.path_regex, nr.path_format, nr.param_convertors = compile_path(
                    nr.path_format)
            # S6.1 修复：拷贝的路由保留原 dependency_overrides_provider，
            # 导致 app.dependency_overrides 对 v1 路由无效；指向 app 并重建
            # handler（handler 在路由创建时已绑定旧 provider）。
            if hasattr(nr, "dependency_overrides_provider"):
                nr.dependency_overrides_provider = app
                nr.app = request_response(nr.get_route_handler())
            app.router.routes.append(nr)
        else:
            if hasattr(route, "dependency_overrides_provider"):
                route.dependency_overrides_provider = app
                route.app = request_response(route.get_route_handler())
            app.router.routes.append(route)


from .api.v1.ideas import router as ideas_router  # noqa: E402
from .api.v1.risks import router as risks_router  # noqa: E402
from .api.v1.exports import router as exports_router  # noqa: E402
from .api.v1.feasibility import router as feasibility_router  # noqa: E402
from .api.v1.colloquial import router as colloquial_router  # noqa: E402
from .api.v1.shoot_v2 import router as shoot_v2_router  # noqa: E402

_mount(auth_router)
_mount(ideas_router)
_mount(risks_router)
_mount(exports_router)
_mount(feasibility_router)
_mount(colloquial_router)
_mount(shoot_v2_router)

# Phase 2：API v1（组织 / RBAC / 租户隔离新体系），不影响现有 /api/* 接口
try:
    from .api.v1.orgs import router as orgs_router  # noqa: E402
    from .api.v1.works import router as works_router  # noqa: E402
    from .api.v1.ai_tasks import router as ai_tasks_router  # noqa: E402
    from .api.v1.ai_config import router as ai_config_router  # noqa: E402  # 123/124
    from .api.v1.ai_models import router as ai_models_router  # noqa: E402  # 128/129
    from .api.v1.ai_stats import router as ai_stats_router  # noqa: E402  # 133/134
    from .api.v1.metrics import router as metrics_router  # noqa: E402  # S9.3
    from .api.v1.enterprise import router as enterprise_router  # noqa: E402  # S10
    _mount(orgs_router, prefix="/api/v1")
    _mount(works_router, prefix="/api/v1")
    _mount(ai_tasks_router, prefix="/api/v1")
    _mount(ai_config_router, prefix="/api/v1")
    _mount(ai_models_router, prefix="/api/v1")
    _mount(ai_stats_router, prefix="/api/v1")
    _mount(metrics_router, prefix="/api/v1")
    _mount(enterprise_router, prefix="/api/v1")
except Exception as e:  # noqa: BLE001
    log.info(f"API v1 模块加载跳过（并行施工中）: {type(e).__name__}: {e}")

# S6.1：事实库 API（独立 try 块，与审核闭环组解耦）
try:
    from .api.v1.facts import router as facts_router  # noqa: E402  # S6.1
    from .api.v1.fact_import import router as fact_import_router  # noqa: E402
    from .api.v1.products import router as products_router  # noqa: E402  # V3.0 B-1
    from .api.v1.copyopt import router as copyopt_router  # noqa: E402  # V3.0 D
    from .api.v1.plan import router as plan_router  # noqa: E402  # V3.0 C
    from .api.v1.platforms import router as platforms_router  # noqa: E402  # V3.0 E
    _mount(facts_router, prefix="/api/v1")
    _mount(fact_import_router, prefix="/api/v1")
    _mount(products_router, prefix="/api/v1")
    _mount(copyopt_router, prefix="/api/v1")
    _mount(plan_router, prefix="/api/v1")
    _mount(platforms_router, prefix="/api/v1")
except Exception as e:  # noqa: BLE001
    log.info(f"事实库 API 加载跳过: {type(e).__name__}: {e}")

# 海报 AI 生图 API（产品文档导入 5 项补齐）
try:
    from .api.v1.poster_image import router as poster_image_router  # noqa: E402
    _mount(poster_image_router, prefix="/api/v1")
except Exception as e:  # noqa: BLE001
    log.info(f"海报生图 API 加载跳过: {type(e).__name__}: {e}")

# S8.1/S8.2/S8.3：媒体资产 + 分镜时间轴 API
try:
    from .api.v1.assets import router as assets_router  # noqa: E402  # S8 媒体
    from .api.v1.timeline import router as timeline_router  # noqa: E402
    _mount(assets_router, prefix="/api/v1")
    _mount(timeline_router, prefix="/api/v1")
except Exception as e:  # noqa: BLE001
    log.info(f"媒体资产 API 加载跳过: {type(e).__name__}: {e}")

# S6.2-S6.5：审核闭环 API（并行组施工中）
try:
    from .api.v1 import reviews as reviews_mod  # noqa: E402  # S6 审核闭环
    _mount(reviews_mod.router, prefix="/api/v1")
    _mount(reviews_mod.review_router, prefix="/api/v1")
    _mount(reviews_mod.finding_router, prefix="/api/v1")
    _mount(reviews_mod.waiver_router, prefix="/api/v1")
    _mount(reviews_mod.delivery_router, prefix="/api/v1")
except Exception as e:  # noqa: BLE001
    log.info(f"审核闭环 API 加载跳过（并行施工中）: {type(e).__name__}: {e}")

# Phase 9：安全纵深（API Key / 审计 / CSP 上报）
try:
    from .api.v1.api_keys import router as api_keys_router  # noqa: E402  # 9-4
    from .api.v1.audit import router as audit_router  # noqa: E402  # 9-5
    from .api.v1.security import router as security_router  # noqa: E402  # 9-1
    _mount(api_keys_router, prefix="/api/v1")
    _mount(audit_router, prefix="/api/v1")
    _mount(security_router, prefix="/api/v1")
except Exception as e:  # noqa: BLE001
    log.info(f"安全 API 加载跳过: {type(e).__name__}: {e}")


# ---------- 基础路由 ----------

@app.get("/api/health")
def health():
    """S9.3：增强健康检查。

    返回：status、version(git commit)、database(连通性)、
    migrations(当前 head)、uptime(秒)。
    """
    from .core import deps as _deps
    ai = getattr(_deps.get_ai_service, "_singleton", None)

    # version：git commit 短哈希（取不到则为 unknown）
    version = "unknown"
    try:
        import subprocess
        version = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3,
            cwd=BASE_DIR,
        ).stdout.strip() or "unknown"
    except Exception:
        pass

    # database：连通性
    db_status = "unknown"
    try:
        from .auth.database import get_session_factory
        s = get_session_factory()()
        try:
            s.execute(__import__("sqlalchemy").text("SELECT 1"))
            db_status = "ok"
        finally:
            s.close()
    except Exception as e:  # noqa: BLE001
        db_status = f"error: {type(e).__name__}"

    # migrations：当前 head
    migration_head = "unknown"
    try:
        versions_dir = os.path.join(BASE_DIR, "migrations", "versions")
        if os.path.isdir(versions_dir):
            revs = sorted(
                f[:-3] for f in os.listdir(versions_dir) if f.endswith(".py")
            )
            migration_head = revs[-1] if revs else "none"
    except Exception:
        pass

    return {
        "status": "ok" if db_status == "ok" else "degraded",
        "version": version,
        "database": db_status,
        "migrations": migration_head,
        "uptime_seconds": round(_time.time() - _START_TIME, 1),
        "model": getattr(ai, "model", DEFAULT_MODEL),
        "ai_ready": ai is not None,
        "calls": ai.call_count if ai else 0,
        "total_tokens": ai.total_tokens if ai else 0,
    }


@app.get("/api/ready")
def ready():
    """S9.3：就绪检查 —— DB 可写才算就绪（k8s readiness 用）。"""
    try:
        from sqlalchemy import text
        from .auth.database import get_session_factory
        s = get_session_factory()()
        try:
            # 可写检查：建表已由 startup 完成，这里只做一次轻量写入探测
            # 用 ai_calls 的 count 做只读探测 + 事务回滚做写探测
            s.execute(text("SELECT count(*) FROM ai_calls"))
            s.execute(text("CREATE TEMP TABLE IF NOT EXISTS _ready_probe (id INTEGER)"))
            s.execute(text("INSERT INTO _ready_probe (id) VALUES (1)"))
            s.rollback()  # 回滚，不留痕迹
        finally:
            s.close()
        return {"ready": True}
    except Exception as e:  # noqa: BLE001
        return {"ready": False, "reason": f"{type(e).__name__}: {e}"}


@app.get("/")
def landing():
    # 公开展示页：未登录用户的默认入口（三层架构第一层）
    return FileResponse(os.path.join(FRONTEND_DIR, "landing.html"))


@app.get("/app")
@app.get("/login")
def workbench():
    # 创作工作台：/app 直接进入；/login 落到登录视图（前端 init 按 pathname 决定）
    # 登录态由 httpOnly refresh Cookie 恢复，未登录访问受保护视图会被带到登录页
    return FileResponse(os.path.join(FRONTEND_DIR, "app.html"))


if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
