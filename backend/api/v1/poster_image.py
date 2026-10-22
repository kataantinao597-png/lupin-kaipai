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

安全：base URL 走 SSRF 校验；key 只走服务端，不落库、不打日志。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ...auth.deps import get_current_user
from ...auth.models import User
from ...auth.database import get_db
from ...core import error_codes
from ...core.exceptions import BusinessError
from ...core.response import ApiResponse, success
from ...models import PosterImageUsage
from ...rbac import ROLE_ADMIN, ROLE_RANK, get_membership
from ...security.ssrf import validate_outbound_url, SSRFError

router = APIRouter(prefix="/poster", tags=["v1-poster-image"])
logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://api.openai.com"
DEFAULT_MODEL = "gpt-image-1"
DEFAULT_FALLBACK_MODEL = "gpt-image-2"
REQUEST_TIMEOUT = 120.0

#: 普通用户海报生图每日限额（2026-10-22 用户拍板）；admin/owner 豁免
POSTER_IMAGE_DAILY_LIMIT = 5


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


def _is_quota_exempt(role: Optional[str]) -> bool:
    """admin/owner 豁免每日限额。"""
    return bool(role) and ROLE_RANK.get(role, 0) >= ROLE_RANK[ROLE_ADMIN]


def _today_start_utc() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def get_poster_quota(db: Session, user_id: str, org_id: str,
                     role: Optional[str]) -> dict:
    """当前用户今日海报生图额度。返回 {limited, limit, used, remaining}。"""
    if _is_quota_exempt(role):
        return {"limited": False, "limit": None, "used": None,
                "remaining": None}
    used = (db.query(func.count(PosterImageUsage.id))
              .filter(PosterImageUsage.user_id == user_id,
                      PosterImageUsage.created_at >= _today_start_utc())
              .scalar() or 0)
    return {"limited": True, "limit": POSTER_IMAGE_DAILY_LIMIT,
            "used": used,
            "remaining": max(0, POSTER_IMAGE_DAILY_LIMIT - used)}


def _check_poster_quota(db: Session, user_id: str, org_id: str,
                        role: Optional[str]) -> None:
    """超限则抛 429（AI_RATE_LIMITED）。豁免角色直接过。"""
    q = get_poster_quota(db, user_id, org_id, role)
    if q["limited"] and q["remaining"] <= 0:
        raise BusinessError(
            f"今日海报生成次数已用完（普通用户每天 {POSTER_IMAGE_DAILY_LIMIT} 次）",
            code=error_codes.AI_RATE_LIMITED,
            http_status=status.HTTP_429_TOO_MANY_REQUESTS)


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
    """当前用户今日海报生图剩余额度（前端展示用）。"""
    ms = get_membership(db, user.id, org_id)
    if ms is None:
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    return success(get_poster_quota(db, user.id, org_id, ms.role))


class ImageGenReq(BaseModel):
    org_id: str = Field(..., description="组织 ID（租户隔离）")
    prompt: str = Field(..., min_length=1, max_length=500,
                        description="图片提示词")
    size: str = Field(default="1024x1024", pattern=r"^\d+x\d+$")


class ImageGenOut(BaseModel):
    image_b64: Optional[str] = None
    image_url: Optional[str] = None
    mime: str = "image/png"


def _call_image_api_once(base: str, key: str, model: str,
                         prompt: str, size: str) -> dict:
    """单通道调用 OpenAI 兼容 images 接口。返回原始 data[0]。

    可重试类失败（超时/连接失败/429/5xx）抛 _RetryableUpstreamError（外层切备选）；
    400（提示词等客户端问题）/401/403（key 配置问题）/SSRF 校验失败直接抛
    BusinessError，不切备选——配置错误不应被备选掩盖。
    """
    import httpx
    proxy = os.environ.get("LUPIN_HTTP_PROXY", "").strip() or None
    # base 可能带 /v1（如 https://deepkey.top/v1）也可能不带（如 https://api.openai.com），
    # 统一归一化，避免拼出 /v1/v1 导致 404
    api_root = base[:-3] if base.lower().endswith("/v1") else base
    try:
        # 走显式代理时跳过本机 DNS 判内网：真实连接只打到代理，
        # 本机解析结果（如沙箱出口 IP 段）与实际出口无关，会误杀合法域名
        url = validate_outbound_url(api_root + "/v1/images/generations",
                                    purpose="图片生成",
                                    via_proxy=bool(proxy))
    except SSRFError as e:
        raise BusinessError(f"图片服务地址不合法：{e}",
                            code=error_codes.INVALID_PARAM,
                            http_status=400) from e
    try:
        with httpx.Client(trust_env=False, proxy=proxy,
                          timeout=REQUEST_TIMEOUT) as cli:
            r = cli.post(
                url,
                headers={"Authorization": "Bearer " + key,
                         "Content-Type": "application/json"},
                json={"model": model, "prompt": prompt, "size": size,
                      "n": 1, "response_format": "b64_json"},
            )
    except Exception as e:  # 连接/超时/DNS 等
        raise _RetryableUpstreamError(
            f"图片服务调用失败：{type(e).__name__}") from e
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


def _call_image_api(prompt: str, size: str) -> dict:
    """主通道失败（超时/429/5xx/连接失败）时自动切备选通道。

    未配主 key 时 503；主备都失败时抛备选通道的错误（BusinessError）。
    """
    base, key, model = _image_conf()
    if not key:
        raise BusinessError("未配置图片生成服务（LUPIN_IMAGE_API_KEY）",
                            code=error_codes.SERVICE_UNAVAILABLE,
                            http_status=status.HTTP_503_SERVICE_UNAVAILABLE)
    try:
        return _call_image_api_once(base, key, model, prompt, size)
    except _RetryableUpstreamError as primary_err:
        fb_base, fb_key, fb_model = _image_fallback_conf()
        if not fb_key:
            raise BusinessError(f"图片服务调用失败：{primary_err}",
                                code=error_codes.AI_PROVIDER_ERROR,
                                http_status=502) from primary_err
        # key 不打日志，只记通道名
        logger.warning("海报生图主通道失败（%s），自动切备选通道",
                       primary_err)
        try:
            first = _call_image_api_once(fb_base, fb_key, fb_model,
                                         prompt, size)
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
    _check_poster_quota(db, user.id, req.org_id, ms.role)
    first = _call_image_api(req.prompt.strip(), req.size)
    db.add(PosterImageUsage(user_id=user.id, org_id=req.org_id))
    db.commit()
    return success(ImageGenOut(
        image_b64=first.get("b64_json"),
        image_url=first.get("url"),
    ).model_dump())
