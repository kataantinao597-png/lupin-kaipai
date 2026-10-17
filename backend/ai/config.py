"""122：模型配置。每次 AI 调用必须明确记录实际生效的配置。

禁止静默回退：如果调用方指定了模型，网关必须用该模型；
模型不可用时明确报错，不允许偷偷换成默认模型。
"""
import hashlib
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ModelConfig:
    """一次 AI 调用的完整配置快照。"""
    provider: str          # 如 "deepkey"
    model: str             # 如 "gpt-6-luna"
    base_url: str          # 实际生效的 endpoint（含 /chat/completions）
    api_key_present: bool  # 只记录"有没有"，绝不记录 Key 本身
    requested_model: Optional[str] = None  # 调用方指定的模型（None=用默认）
    config_version: str = field(default="")  # 配置版本号，见 compute_version

    def __post_init__(self):
        if not self.config_version:
            object.__setattr__(self, "config_version",
                               self.compute_version(self.provider, self.model, self.base_url))

    @staticmethod
    def compute_version(provider: str, model: str, base_url: str) -> str:
        """配置版本 = provider+model+base_url 的短哈希。配置变了版本就变。"""
        raw = f"{provider}|{model}|{base_url}".encode()
        return hashlib.sha256(raw).hexdigest()[:12]

    def log_dict(self) -> dict:
        """可安全记入日志/数据库的字典（不含任何密钥）。"""
        return {
            "provider": self.provider,
            "model": self.model,
            "config_version": self.config_version,
            "requested_model": self.requested_model,
            "api_key_present": self.api_key_present,
        }


def detect_provider(base_url: str) -> str:
    """从 base_url 推断 provider 名称。"""
    u = base_url.lower()
    if "deepkey.top" in u:
        return "deepkey"
    if "openai.com" in u:
        return "openai"
    if "api.deepseek.com" in u:
        return "deepseek"
    # 兜底：取 host
    try:
        from urllib.parse import urlparse
        return urlparse(base_url).netloc or "custom"
    except Exception:
        return "custom"


def build_config(api_key: Optional[str], base_url: Optional[str],
                 requested_model: Optional[str] = None) -> ModelConfig:
    """122：从 AIService 的实际生效值构建 ModelConfig。

    api_key：实际使用的 key（只判断有无，不存储）。
    base_url：AIService.endpoint（已做 deepkey 兜底后的最终值）。
    requested_model：调用方通过 X-Lupin-Model 指定的模型。
    """
    from ..ai_service import BASE_URL, DEFAULT_MODEL
    endpoint = base_url or os.environ.get("LUPIN_API_BASE", BASE_URL)
    # 与 AIService.__init__ 保持一致的 endpoint 归一化
    raw = endpoint.rstrip("/")
    if raw.endswith("/chat/completions"):
        final_endpoint = raw
    else:
        final_endpoint = raw + "/chat/completions"
    final_endpoint = final_endpoint.replace(
        "https://deepkey.top/chat/completions",
        "https://deepkey.top/v1/chat/completions")
    model = requested_model or os.environ.get("LUPIN_MODEL", DEFAULT_MODEL)
    return ModelConfig(
        provider=detect_provider(final_endpoint),
        model=model,
        base_url=final_endpoint,
        api_key_present=bool(api_key and api_key.strip()),
        requested_model=requested_model,
    )
