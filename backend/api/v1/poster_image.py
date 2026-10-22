"""海报 AI 生图 API（v4 海报制作「AI 生成海报」）。

- GET  /api/v1/poster/image/status   图片生成服务是否已配置（前端据此置灰按钮）
- GET  /api/v1/poster/image/quota    当前用户今日生图剩余额度
- POST /api/v1/poster/image          转发 OpenAI 兼容的 /v1/images/generations

配置（环境变量）：
- LUPIN_IMAGE_API_BASE  默认 https://api.openai.com
- LUPIN_IMAGE_API_KEY   未配置时接口 503，前端按钮置灰并提示，绝不伪造图片
- LUPIN_IMAGE_MODEL     默认 gpt-image-1
- LUPIN_HTTP_PROXY      出站代理（本地开发经 hatch-egress-proxy 时设置）

限额：普通用户每天 5 次（admin/owner 豁免），超限 429；用量记 poster_image_usage 表。

安全：base URL 走 SSRF 校验；key 只走服务端，不落库、不打日志。
"""
from __future__ import annotations

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

DEFAULT_BASE = "https://api.openai.com"
REQUEST_TIMEOUT = 120.0

#: 普通用户海报生图每日限额（2026-10-22 用户拍板）；admin/owner 豁免
POSTER_IMAGE_DAILY_LIMIT = 5


def _image_conf() -> tuple[str, str, str]:
    base = os.environ.get("LUPIN_IMAGE_API_BASE", DEFAULT_BASE).strip().rstrip("/") \
        or DEFAULT_BASE
    key = os.environ.get("LUPIN_IMAGE_API_KEY", "").strip()
    model = os.environ.get("LUPIN_IMAGE_MODEL", "gpt-image-1").strip() \
        or "gpt-image-1"
    return base, key, model


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
    return success({"configured": bool(key), "base": base})


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


def _call_image_api(base: str, key: str, model: str,
                    prompt: str, size: str) -> dict:
    """调用 OpenAI 兼容 images 接口。返回原始 data[0]。"""
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
        raise BusinessError(f"图片服务调用失败：{type(e).__name__}",
                            code=error_codes.AI_PROVIDER_ERROR,
                            http_status=502) from e
    if r.status_code >= 400:
        # 不回传上游原文（可能含敏感信息），只给状态码
        raise BusinessError(f"图片服务返回 {r.status_code}，请检查配置",
                            code=error_codes.AI_PROVIDER_ERROR,
                            http_status=502)
    try:
        data = r.json().get("data", [])
        first = data[0] if data else {}
    except Exception as e:
        raise BusinessError("图片服务返回无法解析",
                            code=error_codes.AI_PROVIDER_ERROR,
                            http_status=502) from e
    if not isinstance(first, dict) or \
            not (first.get("b64_json") or first.get("url")):
        raise BusinessError("图片服务未返回图片数据",
                            code=error_codes.AI_PROVIDER_ERROR,
                            http_status=502)
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
    """
    base, key, model = _image_conf()
    if not key:
        raise BusinessError("未配置图片生成服务（LUPIN_IMAGE_API_KEY）",
                            code=error_codes.SERVICE_UNAVAILABLE,
                            http_status=status.HTTP_503_SERVICE_UNAVAILABLE)
    ms = get_membership(db, user.id, req.org_id)
    if ms is None:
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    _check_poster_quota(db, user.id, req.org_id, ms.role)
    first = _call_image_api(base, key, model, req.prompt.strip(), req.size)
    db.add(PosterImageUsage(user_id=user.id, org_id=req.org_id))
    db.commit()
    return success(ImageGenOut(
        image_b64=first.get("b64_json"),
        image_url=first.get("url"),
    ).model_dump())
