"""024：统一服务配置类。

所有环境变量收敛到 Settings（Pydantic Settings），启动时一次性验证。
各模块不再直接 os.environ 散读配置。

必需配置缺失时给出明确错误，而不是运行时才报 NoneType。
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """服务全局配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # 未知环境变量忽略，不报错
    )

    # ---------- 数据库 ----------
    DATABASE_URL: str = Field(
        default="",
        description="数据库连接串。空则使用 backend/data/lupin.db（SQLite 过渡方案）",
    )
    LUPIN_DB_AUTO_CREATE: str = Field(default="1")

    # ---------- 认证 ----------
    LUPIN_JWT_SECRET: str = Field(
        default="",
        description="JWT 签名密钥。生产环境必须配置，否则用临时随机密钥（重启失效）",
    )

    # ---------- AI 模型 ----------
    LUPIN_API_BASE: str = Field(
        default="https://deepkey.top/v1/chat/completions",
        description="模型 API 地址（OpenAI 兼容）",
    )
    LUPIN_API_KEY: str = Field(
        default="",
        description="模型 API Key。未配置时 AI 接口返回 503，前端可引导用户自带 Key",
    )
    LUPIN_MODEL: str = Field(
        default="gpt-6-luna",
        description="默认模型名称",
    )

    # ---------- 服务 ----------
    PORT: int = Field(default=8080)
    ENV: str = Field(default="development", description="development | staging | production")

    @field_validator("LUPIN_API_BASE")
    @classmethod
    def _normalize_api_base(cls, v: str) -> str:
        # deepkey 地址兜底：自动补 /v1（与 ai_service 保持一致）
        v = (v or "").strip().rstrip("/")
        if v.endswith("/chat/completions"):
            return v
        if v in ("https://deepkey.top", "https://deepkey.top/"):
            return "https://deepkey.top/v1/chat/completions"
        return v

    @property
    def is_production(self) -> bool:
        return self.ENV.lower() == "production"

    def validate_startup(self) -> list[str]:
        """启动时校验，返回警告列表（致命错误直接抛异常）。"""
        warnings: list[str] = []
        if self.is_production:
            if not self.LUPIN_JWT_SECRET:
                raise RuntimeError(
                    "生产环境必须配置 LUPIN_JWT_SECRET，否则 JWT 会话在重启后全部失效"
                )
            if not self.LUPIN_API_KEY:
                warnings.append(
                    "LUPIN_API_KEY 未配置：AI 接口将返回 503，用户需自带 Key"
                )
        else:
            if not self.LUPIN_JWT_SECRET:
                warnings.append("LUPIN_JWT_SECRET 未配置：已生成临时密钥，重启后会话失效")
            if not self.LUPIN_API_KEY:
                warnings.append("LUPIN_API_KEY 未配置：AI 接口将返回 503")
        return warnings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """全局单例配置。"""
    return Settings()
