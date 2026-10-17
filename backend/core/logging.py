"""021：统一日志。

JSON 结构化日志：每条记录带时间、级别、模块、事件，生产环境可直接采集。
敏感字段（API Key、token、密码）自动脱敏，绝不进日志。
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone


# 脱敏规则：key/token/password/secret 等字段的值替换为 ***
_SENSITIVE_RE = re.compile(
    r"\"(api[_-]?key|token|password|secret|authorization)\"\s*:\s*\"[^\"]*\"",
    re.IGNORECASE,
)


def _redact(text: str) -> str:
    return _SENSITIVE_RE.sub(r'"\1": "***"', text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        if hasattr(record, "request_id"):
            payload["request_id"] = record.request_id
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)[-500:]
        return _redact(json.dumps(payload, ensure_ascii=False))


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f"lupin.{name}")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


# 模块级默认 logger
log = get_logger("core")
