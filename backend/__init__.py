"""鲁品开拍后端包。

在任何 backend.* 子模块导入之前，把项目根目录的 .env 注入 os.environ。

存在的必要性：项目里有两套配置读取方式——
  1. backend/core/config.py 用 pydantic-settings（自身会读 .env，只进模型不进环境变量）
  2. auth/config.py、api/v1/poster_image.py、media/signing.py、ai_service.py 等直接读 os.environ
方式 2 读不到 .env，导致"按 .env.example 配好了却不生效"。
本模块在包导入的第一时间加载 .env，让两套方式看到同一份配置。

load_dotenv 默认 override=False：已存在的真实环境变量优先，
因此生产环境（控制台注入环境变量）行为不变，只有缺失项才由 .env 补齐。
"""
from __future__ import annotations

import os
import sys

_ENV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
)

# 测试环境不加载 .env：
#   - 断言"未配置 AI 时诚实返回 503"的用例靠 monkeypatch.delenv 清 key，
#     若此处又从 .env 补回来，用例会被开发者的真实 Key 破坏；
#   - 更重要的是避免任何测试真的发起付费模型调用。
# pytest 会先于 backend 被导入，因此用 sys.modules 判断是可靠的。
if "pytest" in sys.modules:
    pass
else:
    try:
        from dotenv import load_dotenv

        if os.path.isfile(_ENV_PATH):
            load_dotenv(_ENV_PATH, override=False)
    except Exception:  # noqa: BLE001  # dotenv 缺失或 .env 解析失败都不应阻断启动
        pass
