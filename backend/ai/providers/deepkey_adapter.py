"""127：deepkey Provider 适配器（OpenAI 兼容协议）。

当前唯一的生产 provider。URL 归一化（/v1 兜底）仍在 AIService.__init__
里做，adapter 只负责：OpenAI 风格请求体构建 + 响应解析。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ...ai_service import AIFatalError
from .base import ProviderAdapter, ProviderCapabilities


class DeepKeyAdapter(ProviderAdapter):
    """deepkey（OpenAI 兼容 /chat/completions）适配器。"""

    @property
    def name(self) -> str:
        return "deepkey"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            text=True,
            vision=False,               # 当前模型不声明视觉能力
            structured_output=True,     # 支持 response_format json_object
            streaming=False,            # 网关暂未使用流式
            max_context_tokens=0,       # 未知：以 model_capabilities 表为准
        )

    def build_payload(self, model: str, messages: List[dict],
                      temperature: float = 0.5,
                      json_object: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "messages": messages,
        }
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def single_call(self, payload: Dict[str, Any]) -> Tuple[str, dict]:
        data = self._transport._post(payload)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise AIFatalError(
                "模型服务返回格式无效：缺少 choices[0].message.content") from e
        if not isinstance(content, str):
            raise AIFatalError(
                f"模型服务返回格式无效：content 不是字符串（{type(content).__name__}）")
        usage = data.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}
        return content, usage
