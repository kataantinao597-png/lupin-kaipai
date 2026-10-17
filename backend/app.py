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
from .core.response import error as error_envelope
from .core import error_codes
from .core.exceptions import AppError, LupinError

log = get_logger("app")

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
    for route in router.routes:
        if prefix:
            nr = copy.copy(route)
            nr.path = prefix + route.path
            # 同步 path_format（Starlette 内部用）并重新编译匹配正则
            if hasattr(nr, "path_format"):
                nr.path_format = prefix + route.path_format
                nr.path_regex, nr.path_format, nr.param_convertors = compile_path(
                    nr.path_format)
            app.router.routes.append(nr)
        else:
            app.router.routes.append(route)


from .api.v1.ideas import router as ideas_router  # noqa: E402
from .api.v1.risks import router as risks_router  # noqa: E402
from .api.v1.exports import router as exports_router  # noqa: E402
from .api.v1.feasibility import router as feasibility_router  # noqa: E402

_mount(auth_router)
_mount(ideas_router)
_mount(risks_router)
_mount(exports_router)
_mount(feasibility_router)

# Phase 2：API v1（组织 / RBAC / 租户隔离新体系），不影响现有 /api/* 接口
try:
    from .api.v1.orgs import router as orgs_router  # noqa: E402
    from .api.v1.works import router as works_router  # noqa: E402
    from .api.v1.ai_tasks import router as ai_tasks_router  # noqa: E402
    from .api.v1.ai_config import router as ai_config_router  # noqa: E402  # 123/124
    from .api.v1.ai_stats import router as ai_stats_router  # noqa: E402  # 133/134
    _mount(orgs_router, prefix="/api/v1")
    _mount(works_router, prefix="/api/v1")
    _mount(ai_tasks_router, prefix="/api/v1")
    _mount(ai_config_router, prefix="/api/v1")
    _mount(ai_stats_router, prefix="/api/v1")
except Exception as e:  # noqa: BLE001
    log.info(f"API v1 模块加载跳过（并行施工中）: {type(e).__name__}: {e}")


# ---------- 基础路由 ----------

@app.get("/api/health")
def health():
    from .core import deps as _deps
    ai = getattr(_deps.get_ai_service, "_singleton", None)
    return {
        "status": "ok",
        "model": getattr(ai, "model", DEFAULT_MODEL),
        "ai_ready": ai is not None,
        "calls": ai.call_count if ai else 0,
        "total_tokens": ai.total_tokens if ai else 0,
    }


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
