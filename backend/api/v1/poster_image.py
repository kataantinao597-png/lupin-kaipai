"""海报 AI 生图 API（v4 海报制作「AI 生成海报」）。

- GET  /api/v1/poster/image/status   图片生成服务是否已配置（前端据此置灰按钮）
- POST /api/v1/poster/image          转发 OpenAI 兼容的 /v1/images/generations

配置（环境变量）：
- LUPIN_IMAGE_API_BASE  默认 https://api.openai.com
- LUPIN_IMAGE_API_KEY   未配置时接口 503，前端按钮置灰并提示，绝不伪造图片
- LUPIN_IMAGE_MODEL     默认 gpt-image-1
- LUPIN_HTTP_PROXY      出站代理（本地开发经 hatch-egress-proxy 时设置）

安全：base URL 走 SSRF 校验；key 只走服务端，不落库、不打日志。
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...auth.deps import get_current_user
from ...auth.models import User
from ...auth.database import get_db
from ...core import error_codes
from ...core.exceptions import BusinessError
from ...core.response import ApiResponse, success
from ...rbac import get_membership
from ...security.ssrf import validate_outbound_url, SSRFError

router = APIRouter(prefix="/poster", tags=["v1-poster-image"])

DEFAULT_BASE = "https://api.openai.com"
REQUEST_TIMEOUT = 120.0


def _image_conf() -> tuple[str, str, str]:
    base = os.environ.get("LUPIN_IMAGE_API_BASE", DEFAULT_BASE).strip().rstrip("/") \
        or DEFAULT_BASE
    key = os.environ.get("LUPIN_IMAGE_API_KEY", "").strip()
    model = os.environ.get("LUPIN_IMAGE_MODEL", "gpt-image-1").strip() \
        or "gpt-image-1"
    return base, key, model


@router.get("/image/status", response_model=ApiResponse[dict])
def image_status(user: User = Depends(get_current_user)):
    """图片生成服务是否已配置。未配置 → 前端置灰按钮。"""
    base, key, _model = _image_conf()
    return success({"configured": bool(key), "base": base})


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
    """AI 生成海报配图。未配置 key 时 503（前端置灰，不伪造）。"""
    base, key, model = _image_conf()
    if not key:
        raise BusinessError("未配置图片生成服务（LUPIN_IMAGE_API_KEY）",
                            code=error_codes.SERVICE_UNAVAILABLE,
                            http_status=status.HTTP_503_SERVICE_UNAVAILABLE)
    ms = get_membership(db, user.id, req.org_id)
    if ms is None:
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    first = _call_image_api(base, key, model, req.prompt.strip(), req.size)
    return success(ImageGenOut(
        image_b64=first.get("b64_json"),
        image_url=first.get("url"),
    ).model_dump())
