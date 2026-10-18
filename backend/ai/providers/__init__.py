"""127：Provider 适配器包。

网关通过适配器调用模型，不再直拼 URL / 直接解析响应。
新增 Provider 时：继承 ProviderAdapter，实现能力声明/请求构建/响应解析，
在网关构造时传入即可。
"""
from .base import ProviderAdapter, ProviderCapabilities
from .deepkey_adapter import DeepKeyAdapter

__all__ = ["ProviderAdapter", "ProviderCapabilities", "DeepKeyAdapter"]
