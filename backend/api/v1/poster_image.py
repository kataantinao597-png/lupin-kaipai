"""海报 AI 生图 API（v4 海报制作「AI 生成海报」）。

- GET  /api/v1/poster/image/status   图片生成服务是否已配置（前端据此置灰按钮）
- GET  /api/v1/poster/image/quota    当前用户今日生图剩余额度
- POST /api/v1/poster/image          转发 OpenAI 兼容的 /v1/images/generations

配置（环境变量）：
- LUPIN_IMAGE_API_BASE  默认 https://api.openai.com
- LUPIN_IMAGE_API_KEY   未配置时接口 503，前端按钮置灰并提示，绝不伪造图片
- LUPIN_IMAGE_MODEL     默认 gpt-image-1
- LUPIN_IMAGE_API_BASE_FALLBACK / LUPIN_IMAGE_API_KEY_FALLBACK /
  LUPIN_IMAGE_MODEL_FALLBACK  备选通道（2026-10-22 用户拍板：主 luna 超时/429/5xx/
  连接失败时自动切备选，不配备选 key 则无备选）
- LUPIN_HTTP_PROXY      出站代理（本地开发经 hatch-egress-proxy 时设置）

限额：普通用户每天 5 次（admin/owner 豁免），超限 429；用量记 poster_image_usage 表。
计数与豁免判定统一走 backend/services/poster_quota.py（Agent 上下文读同一口径）。

安全：base URL 走 SSRF 校验；key 只走服务端，不落库、不打日志。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...auth.deps import get_current_user
from ...auth.models import User
from ...auth.database import get_db, get_session_factory
from ...core import error_codes
from ...core.exceptions import BusinessError
from ...core.response import ApiResponse, success
from ...models import PosterImageUsage
from ...rbac import get_membership
from ...security.ssrf import validate_outbound_url, SSRFError
# 唯一口径：限额数量/豁免判定/当日起点都在 poster_quota，本模块只做 HTTP 编排
from ...services.poster_quota import (  # noqa: F401  (常量对外仍可见)
    POSTER_IMAGE_DAILY_LIMIT,
    enforce_quota as _check_poster_quota,
    enforce_quota_with_inflight as _check_poster_quota_inflight,
    get_quota as get_poster_quota,
    get_quota_with_inflight as get_poster_quota_inflight,
)
from ...core.timeouts import (  # O-09：集中常量表
    POSTER_REQUEST_TIMEOUT_SECONDS,
    POSTER_SYNC_TOTAL_TIMEOUT_SECONDS,
)
from ...services import poster_jobs

router = APIRouter(prefix="/poster", tags=["v1-poster-image"])
logger = logging.getLogger(__name__)

#: S1 修复：配额提交锁（单进程内）。异步提交的"校验→建任务"、同步接口的
#: "校验→插占位行"必须在同一临界区内完成，否则两个并发请求会在计数
#: 还是 0 时同时通过校验。临界区内只有 DB 查询 + 内存操作，不含上游
#: API 调用，不影响吞吐。
#: 作用域说明：本锁与 poster_jobs 内存任务表同为进程内状态，与异步任务
#: 系统的单进程假设一致（poster_jobs 模块注释：单进程 uvicorn）。若未来
#: 改为多 worker 部署，须把在途计数与占位行统一收进 DB 约束，本锁不再
#: 充分——该改造超出本次 S1 修复范围，需与任务存储改造一并做。
_quota_submit_lock = threading.Lock()

DEFAULT_BASE = "https://api.openai.com"
DEFAULT_MODEL = "gpt-image-1"
DEFAULT_FALLBACK_MODEL = "gpt-image-2"
#: O-09：以下两名是旧名 alias（对外仍可见），数值见集中常量表。
REQUEST_TIMEOUT = POSTER_REQUEST_TIMEOUT_SECONDS  # 单通道上限（异步任务路径）：主+备最坏 180s，与任务看门狗 200s 对齐
#: M5：同步接口总预算。网关约 60s 即 504，同步路径主+备绝不能再各 90s。
#: 同步调用方传 total_timeout=SYNC_TOTAL_TIMEOUT，主备共享这 55s 预算
#: （主失败快则备选剩得多，主超时则不再切备选），总墙钟 ≤55s + 少量开销。
SYNC_TOTAL_TIMEOUT = POSTER_SYNC_TOTAL_TIMEOUT_SECONDS


class _RetryableUpstreamError(Exception):
    """上游可重试错误（超时/连接失败/429/5xx）：触发备选通道。"""


def _image_conf() -> tuple[str, str, str]:
    base = os.environ.get("LUPIN_IMAGE_API_BASE", DEFAULT_BASE).strip().rstrip("/") \
        or DEFAULT_BASE
    key = os.environ.get("LUPIN_IMAGE_API_KEY", "").strip()
    model = os.environ.get("LUPIN_IMAGE_MODEL", DEFAULT_MODEL).strip() \
        or DEFAULT_MODEL
    return base, key, model


def _image_fallback_conf() -> tuple[str, str, str]:
    """备选通道配置。未配 LUPIN_IMAGE_API_KEY_FALLBACK 时 key 为空 = 无备选。"""
    base, _key, _model = _image_conf()
    fb_base = os.environ.get("LUPIN_IMAGE_API_BASE_FALLBACK",
                             "").strip().rstrip("/") or base
    fb_key = os.environ.get("LUPIN_IMAGE_API_KEY_FALLBACK", "").strip()
    fb_model = os.environ.get("LUPIN_IMAGE_MODEL_FALLBACK",
                              DEFAULT_FALLBACK_MODEL).strip() \
        or DEFAULT_FALLBACK_MODEL
    return fb_base, fb_key, fb_model


@router.get("/image/status", response_model=ApiResponse[dict])
def image_status(user: User = Depends(get_current_user)):
    """图片生成服务是否已配置。未配置 → 前端置灰按钮。"""
    base, key, _model = _image_conf()
    _fb_base, fb_key, _fb_model = _image_fallback_conf()
    return success({"configured": bool(key), "base": base,
                    "fallback_configured": bool(fb_key)})


@router.get("/image/quota", response_model=ApiResponse[dict])
def image_quota(org_id: str = Query(...),
                user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    """当前用户今日海报生图剩余额度（前端展示用）。

    M-7：展示口径与拦截口径一致——用含在途口径（已成功 + 在途），
    否则用户看到“剩余 N 次”提交却吃 429。返回含 inflight 字段，
    前端可据此提示“X 个正在生成”。
    """
    ms = get_membership(db, user.id, org_id)
    if ms is None:
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    return success(get_poster_quota_inflight(db, user.id, org_id, ms.role))


# M3 修复：size 只接受上游支持的固定尺寸，拒绝 999999999x999999999
# 这类超大尺寸透传上游（误触发备选切换与上游限流）。
ALLOWED_IMAGE_SIZES = ("1024x1024", "1024x1536", "1536x1024")


class ImageGenReq(BaseModel):
    org_id: str = Field(..., description="组织 ID（租户隔离）")
    prompt: str = Field(..., min_length=1, max_length=500,
                        description="图片提示词")
    size: Literal["1024x1024", "1024x1536", "1536x1024"] = Field(
        default="1024x1024", description="图片尺寸（固定枚举）")


class ImageGenOut(BaseModel):
    image_b64: Optional[str] = None
    image_url: Optional[str] = None
    mime: str = "image/png"


def _call_image_api_once(base: str, key: str, model: str,
                         prompt: str, size: str,
                         timeout: float = REQUEST_TIMEOUT) -> dict:
    """单通道调用 OpenAI 兼容 images 接口。返回原始 data[0]。

    可重试类失败（超时/连接失败/429/5xx/重定向）抛 _RetryableUpstreamError
    （外层切备选）；400（提示词等客户端问题）/401/403（key 配置问题）/
    SSRF 校验失败直接抛 BusinessError，不切备选——配置错误不应被备选掩盖。

    M2：代理不再豁免内网判定。base 来自服务端环境变量，但校验层必须
    完整：即使走代理，也对目标 hostname 做解析/判定（via_proxy=False），
    字面内网 IP（10.x、172.16-31.x、192.168.x、127.x、::1 等）与解析到
    内网/保留地址的域名一律拒绝，localhost 同理（解析到 127.x 即拦截）。
    fail closed：本机 DNS 与代理出口不一致时宁可误杀合法域名，也不放行
    可能指向内网的目标。security/ssrf.py 的 via_proxy 参数本身未改，
    只是本路径不再使用它；其他调用方不受影响。
    M2（重定向）：显式 follow_redirects=False，绝不跟随上游重定向；
    上游返回 3xx 一律按失败处理并记日志（不校验、不跟随 Location，
    Location 本身可能指向内网，跟随即 SSRF）。
    """
    import httpx
    proxy = os.environ.get("LUPIN_HTTP_PROXY", "").strip() or None
    # base 可能带 /v1（如 https://deepkey.top/v1）也可能不带（如 https://api.openai.com），
    # 统一归一化，避免拼出 /v1/v1 导致 404
    api_root = base[:-3] if base.lower().endswith("/v1") else base
    try:
        url = validate_outbound_url(api_root + "/v1/images/generations",
                                    purpose="图片生成",
                                    via_proxy=False)
    except SSRFError as e:
        raise BusinessError(f"图片服务地址不合法：{e}",
                            code=error_codes.INVALID_PARAM,
                            http_status=400) from e
    try:
        with httpx.Client(trust_env=False, proxy=proxy,
                          timeout=timeout, follow_redirects=False) as cli:
            r = cli.post(
                url,
                headers={"Authorization": "Bearer " + key,
                         "Content-Type": "application/json"},
                json={"model": model, "prompt": prompt, "size": size,
                      "n": 1, "response_format": "b64_json"},
                follow_redirects=False,
            )
    except Exception as e:  # 连接/超时/DNS 等
        raise _RetryableUpstreamError(
            f"图片服务调用失败：{type(e).__name__}") from e
    if r.status_code in (301, 302, 303, 307, 308):
        # 不记录 Location（可能含内网地址），只记状态码
        logger.warning("海报生图上游返回重定向（%s），不跟随、按失败处理",
                       r.status_code)
        raise _RetryableUpstreamError(
            f"图片服务返回重定向 {r.status_code}")
    if r.status_code == 429 or r.status_code >= 500:
        # 不回传上游原文（可能含敏感信息），只给状态码
        raise _RetryableUpstreamError(f"图片服务返回 {r.status_code}")
    if r.status_code >= 400:
        raise BusinessError(f"图片服务返回 {r.status_code}，请检查配置",
                            code=error_codes.AI_PROVIDER_ERROR,
                            http_status=502)
    try:
        data = r.json().get("data", [])
        first = data[0] if data else {}
    except Exception as e:
        raise _RetryableUpstreamError("图片服务返回无法解析") from e
    if not isinstance(first, dict) or \
            not (first.get("b64_json") or first.get("url")):
        raise _RetryableUpstreamError("图片服务未返回图片数据")
    return first


def _call_image_api(prompt: str, size: str,
                    total_timeout: float | None = None) -> dict:
    """主通道失败（超时/429/5xx/连接失败）时自动切备选通道。

    未配主 key 时 503；主备都失败时抛备选通道的错误（BusinessError）。

    total_timeout=None（异步任务路径）：沿用旧行为，单通道各 90s，
    与任务看门狗 200s 对齐，网关 60s 不适用（前端轮询取图）。
    total_timeout=55.0（同步接口路径，M5）：主备共享总预算。只把
    httpx timeout 设成 55 是不够的——主 55s + 备 55s 最坏仍是 110s，
    仍会被网关 504 截断且客户端断开后服务端继续跑完记账。因此这里
    用 deadline 切分：主通道 timeout=总预算，主失败后备选 timeout=
    总预算 - 已耗时；预算耗尽则不再切备选，直接失败。同步接口的
    except 分支会删除占位行（失败不扣额度），超时失败同样不记账。
    """
    base, key, model = _image_conf()
    if not key:
        raise BusinessError("未配置图片生成服务（LUPIN_IMAGE_API_KEY）",
                            code=error_codes.SERVICE_UNAVAILABLE,
                            http_status=status.HTTP_503_SERVICE_UNAVAILABLE)
    deadline = (time.monotonic() + total_timeout
                if total_timeout is not None else None)
    try:
        if total_timeout is None:
            return _call_image_api_once(base, key, model, prompt, size)
        return _call_image_api_once(base, key, model, prompt, size,
                                    timeout=total_timeout)
    except _RetryableUpstreamError as primary_err:
        fb_base, fb_key, fb_model = _image_fallback_conf()
        if not fb_key:
            raise BusinessError(f"图片服务调用失败：{primary_err}",
                                code=error_codes.AI_PROVIDER_ERROR,
                                http_status=502) from primary_err
        # 轻微 #8：备选与主通道完全相同（同 base+key+model）时不是备选，
        # 只是同源重试——直接报主通道错误，不浪费一次同源调用。
        if (fb_base, fb_key, fb_model) == (base, key, model):
            logger.warning("海报生图备选通道与主通道相同，跳过同源重试")
            raise BusinessError(f"图片服务调用失败：{primary_err}",
                                code=error_codes.AI_PROVIDER_ERROR,
                                http_status=502) from primary_err
        # key 不打日志，只记通道名
        logger.warning("海报生图主通道失败（%s），自动切备选通道",
                       primary_err)
        try:
            if total_timeout is None:
                first = _call_image_api_once(fb_base, fb_key, fb_model,
                                             prompt, size)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BusinessError(
                        f"图片服务调用失败：{primary_err}（同步总超时 "
                        f"{total_timeout}s 已耗尽，不再切备选通道）",
                        code=error_codes.AI_PROVIDER_ERROR,
                        http_status=502) from primary_err
                first = _call_image_api_once(fb_base, fb_key, fb_model,
                                             prompt, size, timeout=remaining)
        except _RetryableUpstreamError as fb_err:
            raise BusinessError(f"图片服务主备通道均失败：{fb_err}",
                                code=error_codes.AI_PROVIDER_ERROR,
                                http_status=502) from fb_err
        logger.info("海报生图已走备选通道出图")
        return first


@router.post("/image", response_model=ApiResponse[ImageGenOut])
def generate_poster_image(
    req: ImageGenReq,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """AI 生成海报配图。

    未配置 key 时 503（前端置灰，不伪造）；普通用户每天限 5 次，超限 429；
    admin/owner 豁免。成功一次记一条用量。
    主通道（LUPIN_IMAGE_API_KEY）超时/429/5xx/连接失败时自动切备选通道
    （LUPIN_IMAGE_API_KEY_FALLBACK）。
    """
    ms = get_membership(db, user.id, req.org_id)
    if ms is None:
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    # S1 修复：同步接口同样存在"校验→记账"竞态。改为占位行模式：
    # 锁内校验 + 立即插行提交（占位对并发请求可见），上游调用在锁外；
    # 调用失败则删除占位行（失败不扣额度），成功则保留该行即为记账。
    # 校验必须用含在途的口径（_check_poster_quota_inflight）：同步校验
    # 若只数 DB 已成功数，看不到异步在途任务，可先异步占满 3 个在途再
    # 用同步接口继续刷——两条路径的在途计数必须互可见。
    # 残留风险：进程在调用中途崩溃时占位行泄漏（计一次未出图的额度），
    # 远小于并发绕过无限刷量的风险，接受该取舍。
    with _quota_submit_lock:
        _check_poster_quota_inflight(db, user.id, req.org_id, ms.role)
        usage = PosterImageUsage(user_id=user.id, org_id=req.org_id)
        db.add(usage)
        db.commit()
    try:
        # M5：同步路径总预算 55s（网关约 60s 504，留余量）；超时失败走
        # 下方 except，删除占位行，不记账。异步路径不走这里，见 /image/jobs。
        first = _call_image_api(req.prompt.strip(), req.size,
                                total_timeout=SYNC_TOTAL_TIMEOUT)
        # 轻微 #1：响应序列化必须在 try 内——原先在 try 外，序列化异常
        # 时占位行已 commit 不被删除，用户“已出图却被扣额度”。
        resp = ImageGenOut(
            image_b64=first.get("b64_json"),
            image_url=first.get("url"),
        ).model_dump()
    except Exception:
        # 失败释放占位（失败不扣额度）
        try:
            db.delete(usage)
            db.commit()
        except Exception:
            # 轻微 #2：占位释放失败必须记日志，否则残留不可观测
            db.rollback()
            logger.warning("海报同步占位行释放失败 user=%s org=%s",
                           user.id, req.org_id, exc_info=True)
        raise
    return success(resp)


# ---------------------------------------------------------------------------
# 异步版（推荐）：网关对单请求硬超时约 60s，生图常 30-45s、偶发更久，
# 同步接口会被网关 504 截断。异步接口毫秒级返回 job_id，前端轮询取图。
# ---------------------------------------------------------------------------

class ImageJobOut(BaseModel):
    job_id: str
    status: str


@router.post("/image/jobs", response_model=ApiResponse[ImageJobOut],
             status_code=status.HTTP_202_ACCEPTED)
def create_image_job(
    req: ImageGenReq,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """提交生图任务，立即返回 job_id（不阻塞等图）。配额在提交时校验。

    S1 修复：校验（已成功 + 在途）与建任务必须在 _quota_submit_lock
    内原子完成，否则并发提交会在计数还是 0 时全部通过。任务失败后
    自动离 running 态，在途计数回落；成功后由后台回调记账。
    """
    ms = get_membership(db, user.id, req.org_id)
    if ms is None:
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    with _quota_submit_lock:
        _check_poster_quota_inflight(db, user.id, req.org_id, ms.role)
        prompt = req.prompt.strip()
        size = req.size
        user_id, org_id = user.id, req.org_id
        job = poster_jobs.create_job(user_id, org_id)

    def _work() -> dict:
        # 异步路径不设同步总预算：后台跑、前端轮询，网关 60s 不适用；
        # 单通道仍各 90s，与任务看门狗 200s 对齐。
        return _call_image_api(prompt, size)

    def _record_usage(_first: dict) -> None:
        # 后台线程里用独立会话记账（不能复用请求会话）
        sess = get_session_factory()()
        try:
            sess.add(PosterImageUsage(user_id=user_id, org_id=org_id))
            sess.commit()
        finally:
            sess.close()

    poster_jobs.run(job, _work, _record_usage)
    return success({"job_id": job.id, "status": job.status})


@router.get("/image/jobs/{job_id}", response_model=ApiResponse[dict])
def get_image_job(job_id: str, user: User = Depends(get_current_user)):
    """轮询任务状态：running / done（带 image_b64）/ failed（带错误文案）。"""
    job = poster_jobs.get_job(job_id, user_id=user.id)
    if job is None:
        raise BusinessError("生成任务不存在或已过期，请重新生成",
                            code=error_codes.RESOURCE_NOT_FOUND,
                            http_status=status.HTTP_404_NOT_FOUND)
    return success(job.public())
