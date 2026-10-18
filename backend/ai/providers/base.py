"""127：Provider 适配器抽象。

职责划分：
- transport（AIService）：只负责 HTTP 发送 + HTTP 状态码到异常的映射。
- adapter：在 transport 之上负责：能力声明、请求体构建、响应解析、错误分类。

网关只跟 adapter 打交道，不再直拼 URL、不再直接解析 choices[0]。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class ProviderCapabilities:
    """能力声明。网关/能力注册表用它做调用前校验。"""
    text: bool = True
    vision: bool = False
    structured_output: bool = False  # 是否支持 response_format={"type":"json_object"}
    streaming: bool = False
    max_context_tokens: int = 0  # 0 = 未知/未声明


class ProviderAdapter(ABC):
    """模型 Provider 适配器抽象基类。"""

    def __init__(self, transport):
        """
        transport: 底层 HTTP 传输对象（当前为 ai_service.AIService），
        需提供 _post(payload) -> dict（dict 为解析后的 JSON body）。
        """
        self._transport = transport

    @property
    @abstractmethod
    def name(self) -> str:
        """provider 名称，如 "deepkey"。"""

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """能力声明。"""

    @abstractmethod
    def build_payload(self, model: str, messages: List[dict],
                      temperature: float = 0.5,
                      json_object: bool = False) -> Dict[str, Any]:
        """构建请求体。"""

    @abstractmethod
    def single_call(self, payload: Dict[str, Any]) -> Tuple[str, dict]:
        """单次调用，返回 (content, usage)。

        响应解析失败抛 AIFatalError（结构错误，不重试）。
        """

    def classify_error(self, exc: BaseException) -> str:
        """错误分类：复用 backend/ai/retry.py 的分类策略。

        返回分类字符串：connection / rate_limit / server_error /
        client_error / structure。网关据此决定重试与熔断。
        """
        from ..retry import classify_error
        return classify_error(exc).category
