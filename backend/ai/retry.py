"""125：AI 调用重试策略。

按错误类型区分处理：
- 连接失败（网络不可达/DNS/建连超时）：最多重试 3 次，指数退避
- 429 限流：按 Retry-After 退避（无该头时指数退避），最多重试 3 次
- 5xx 服务端错误：最多重试 2 次，指数退避
- 4xx（非 429，如 400/401/403/404）：不重试，直接抛
- 结构错误（JSON 解析失败/字段缺失）：不重试，直接抛

本模块只做"HTTP 层"重试决策；generate_with_retry 的"校验层"重试不受影响。
"""
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..ai_service import AIFatalError, AIRetryableError


# 重试类别
RETRY_CONNECTION = "connection"   # 连接失败
RETRY_RATE_LIMIT = "rate_limit"   # 429
RETRY_SERVER = "server_error"     # 5xx
NO_RETRY_CLIENT = "client_error"  # 4xx 非 429
NO_RETRY_STRUCTURE = "structure"  # 结构错误

# 各类别最大重试次数
MAX_RETRIES = {
    RETRY_CONNECTION: 3,
    RETRY_RATE_LIMIT: 3,
    RETRY_SERVER: 2,
}


@dataclass
class RetryDecision:
    """单次失败后的重试决策。"""
    should_retry: bool
    wait_seconds: float = 0.0
    reason: str = ""
    category: str = ""


def classify_error(exc: BaseException, status_code: Optional[int] = None,
                   retry_after: Optional[str] = None) -> RetryDecision:
    """125：错误分类。返回是否重试、等待秒数、原因。

    status_code：HTTP 状态码（网络层错误时为 None）。
    retry_after：响应头的 Retry-After 值（秒或 HTTP 日期，这里只解析秒数）。
    """
    # 结构错误：AIFatalError 中明确标记为结构问题的，不重试
    if isinstance(exc, AIFatalError):
        msg = str(exc)
        if any(k in msg for k in ("JSON", "格式无效", "缺少")):
            return RetryDecision(False, reason="结构错误不重试", category=NO_RETRY_STRUCTURE)
        return RetryDecision(False, reason=f"致命错误不重试: {msg[:80]}",
                             category=NO_RETRY_CLIENT)

    if not isinstance(exc, AIRetryableError):
        return RetryDecision(False, reason=f"未知错误类型不重试: {type(exc).__name__}",
                             category=NO_RETRY_CLIENT)

    msg = str(exc).lower()
    # 429 限流
    if status_code == 429 or "429" in msg or "限流" in msg:
        wait = _parse_retry_after(retry_after)
        return RetryDecision(True, wait_seconds=wait, reason="429 限流退避",
                             category=RETRY_RATE_LIMIT)
    # 5xx
    if status_code is not None and 500 <= status_code < 600:
        return RetryDecision(True, wait_seconds=0, reason="5xx 服务端错误",
                             category=RETRY_SERVER)
    if "http 5" in msg:
        return RetryDecision(True, wait_seconds=0, reason="5xx 服务端错误",
                             category=RETRY_SERVER)
    # 其余 AIRetryableError 视为连接类问题（超时/DNS/建连失败）
    return RetryDecision(True, wait_seconds=0, reason="连接失败",
                         category=RETRY_CONNECTION)


def _parse_retry_after(value: Optional[str]) -> float:
    """解析 Retry-After 头。只解析秒数；解析失败默认 5 秒，上限 60 秒。"""
    if not value:
        return 5.0
    try:
        secs = float(value.strip())
        return max(1.0, min(secs, 60.0))
    except (ValueError, TypeError, AttributeError):
        return 5.0


def backoff_seconds(category: str, attempt: int) -> float:
    """指数退避：2^attempt 秒，上限 30 秒。attempt 从 0 开始。"""
    return min(2 ** attempt, 30)


@dataclass
class RetryExecutor:
    """带分类重试的执行器。记录每次尝试，供网关做调用计数和熔断。"""
    on_attempt: Optional[Callable[[int, str], None]] = field(default=None, repr=False)

    attempts: int = field(default=0, init=False)
    last_category: str = field(default="", init=False)

    def run(self, fn: Callable, *args, **kwargs):
        """执行 fn，失败时按策略重试。最终失败抛原始异常。"""
        retries_used = {RETRY_CONNECTION: 0, RETRY_RATE_LIMIT: 0, RETRY_SERVER: 0}
        attempt = 0
        while True:
            self.attempts = attempt + 1
            try:
                return fn(*args, **kwargs)
            except (AIRetryableError, AIFatalError) as e:
                # 从异常中提取状态码（ai_service 的 _post 把状态码写在消息里）
                status = _extract_status(str(e))
                retry_after = getattr(e, "retry_after", None)
                decision = classify_error(e, status_code=status, retry_after=retry_after)
                self.last_category = decision.category
                if self.on_attempt:
                    self.on_attempt(self.attempts, decision.category)
                if not decision.should_retry:
                    raise
                max_n = MAX_RETRIES.get(decision.category, 0)
                retries_used[decision.category] += 1
                if retries_used[decision.category] > max_n:
                    raise
                wait = decision.wait_seconds or backoff_seconds(decision.category, attempt)
                time.sleep(wait)
                attempt += 1


def _extract_status(msg: str) -> Optional[int]:
    """从错误消息中提取 HTTP 状态码，如 'HTTP 429' / 'HTTP 503'。"""
    import re
    m = re.search(r"HTTP\s+(\d{3})", msg)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None
