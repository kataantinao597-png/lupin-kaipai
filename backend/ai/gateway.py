"""121：统一 AI Gateway。

所有业务（创意/分镜/风险/可行性）都走 gateway，不再直接调 ai_service。
ai_service.AIService 保留为底层传输层（HTTP 调用 + 错误映射）。

网关职责：
- 121：统一入口 chat() / generate_with_retry()
- 122：记录实际生效的 provider/model/config_version（日志 + ai_tasks.model_used）
- 124：调用前检查组织预算，超限返回 402
- 125：HTTP 层重试走 backend/ai/retry.py 的分类策略
- 126：单任务最多 5 次调用、最多 500K tokens，超限熔断并告警
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..ai_service import (
    AIFatalError,
    AIConfigError,
    AIRetryableError,
    DEFAULT_MODEL,
)
from .config import ModelConfig, build_config
from .retry import RetryExecutor

logger = logging.getLogger(__name__)

# 126：单任务上限
MAX_CALLS_PER_TASK = 5
MAX_TOKENS_PER_TASK = 500_000


class BudgetExceededError(Exception):
    """124：组织 AI 预算超限。API 层映射为 402。"""


class TaskLimitExceededError(Exception):
    """126：单任务调用次数或 token 超限，熔断。"""


@dataclass
class GatewayCallRecord:
    """122：一次网关调用的可审计记录（不含密钥）。"""
    task_type: str
    provider: str
    model: str
    config_version: str
    requested_model: Optional[str]
    attempts: int
    total_tokens: int
    duration_ms: int
    success: bool
    error: str = ""


class AIGateway:
    """统一 AI 网关。包装一个 AIService（传输层）。"""

    def __init__(self, service, org_id: Optional[str] = None,
                 db=None, task_id: Optional[str] = None,
                 alert_fn: Optional[Callable[[str, dict], None]] = None):
        """
        service: ai_service.AIService 实例（传输层）
        org_id: 组织 ID（124 预算检查用；None 则跳过预算）
        db: SQLAlchemy session（预算扣减 / ai_tasks.model_used 回写用；None 则只记日志）
        task_id: 关联的 ai_tasks.id（122 回写 model_used 用）
        alert_fn: 126 熔断告警回调 fn(message, context)
        """
        self._svc = service
        self._org_id = org_id
        self._db = db
        self._task_id = task_id
        self._alert_fn = alert_fn or (lambda m, c: logger.warning("AI告警: %s %s", m, c))
        # 126：本网关实例内的任务级计数
        self._task_calls = 0
        self._task_tokens = 0

    # ---------- 内部 ----------

    def _model_config(self, requested_model: Optional[str]) -> ModelConfig:
        """122：基于传输层实际生效值构建配置快照。"""
        return build_config(
            api_key=getattr(self._svc, "api_key", None),
            base_url=getattr(self._svc, "endpoint", None),
            requested_model=requested_model,
        )

    def _check_task_limits(self, task_type: str):
        """126：单任务调用次数 / token 上限。"""
        if self._task_calls >= MAX_CALLS_PER_TASK:
            self._alert_fn("单任务调用次数超限熔断",
                           {"task_type": task_type, "calls": self._task_calls,
                            "limit": MAX_CALLS_PER_TASK})
            raise TaskLimitExceededError(
                f"任务 {task_type} 调用次数超限（{self._task_calls}/{MAX_CALLS_PER_TASK}），"
                "已熔断以防止费用失控。请检查模型配置或简化输入后重试。")
        if self._task_tokens >= MAX_TOKENS_PER_TASK:
            self._alert_fn("单任务 token 超限熔断",
                           {"task_type": task_type, "tokens": self._task_tokens,
                            "limit": MAX_TOKENS_PER_TASK})
            raise TaskLimitExceededError(
                f"任务 {task_type} token 消耗超限（{self._task_tokens}/{MAX_TOKENS_PER_TASK}），"
                "已熔断以防止费用失控。")

    def _check_budget(self, task_type: str):
        """124：调用前检查组织预算。超限抛 BudgetExceededError（API 层转 402）。"""
        if not self._org_id or self._db is None:
            return
        from .budget import check_budget
        check_budget(self._db, self._org_id, task_type)

    def _record_config(self, cfg: ModelConfig, task_type: str, attempts: int,
                       tokens: int, duration_ms: int, success: bool, error: str = ""):
        """122：记录实际生效配置到日志；有 db+task_id 时回写 ai_tasks.model_used。"""
        rec = GatewayCallRecord(
            task_type=task_type, provider=cfg.provider, model=cfg.model,
            config_version=cfg.config_version, requested_model=cfg.requested_model,
            attempts=attempts, total_tokens=tokens, duration_ms=duration_ms,
            success=success, error=error[:200],
        )
        logger.info("AI调用 task=%s provider=%s model=%s cfg=%s tokens=%d success=%s",
                    rec.task_type, rec.provider, rec.model, rec.config_version,
                    rec.total_tokens, rec.success)
        if self._db is not None and self._task_id:
            try:
                from ..models import AITask
                task = self._db.get(AITask, self._task_id)
                if task is not None:
                    task.model_used = f"{cfg.provider}/{cfg.model}@{cfg.config_version}"
                    self._db.commit()
            except Exception as e:
                logger.warning("回写 ai_tasks.model_used 失败: %s", e)

    def _charge_budget(self, tokens: int):
        """124：调用成功后按 token 扣减预算（简化计费：按 token 数折算）。"""
        if not self._org_id or self._db is None:
            return
        from .budget import charge_usage
        try:
            charge_usage(self._db, self._org_id, tokens)
        except Exception as e:
            logger.warning("预算扣减失败: %s", e)

    # ---------- 对外 API ----------

    def chat(self, task_type: str, messages: List[dict],
             model: Optional[str] = None,
             temperature: float = 0.5,
             json_object: bool = False) -> Tuple[str, dict]:
        """121：单次 chat 调用。禁止静默回退：指定模型即用该模型。

        返回 (content, usage)。usage 为模型返回的 usage 字典。
        """
        # 126：先检查任务级上限
        self._check_task_limits(task_type)
        # 124：再检查组织预算
        self._check_budget(task_type)
        # 122：配置快照（实际生效值）
        cfg = self._model_config(requested_model=model)
        # 122：禁止静默回退 —— 调用方指定了模型，就必须用它
        effective_model = model or cfg.model

        start = time.time()
        executor = RetryExecutor()
        tokens = 0
        # Phase 5 可观测性钩子：网关调用前后通知 observability 记录/熔断/并发/缓存
        from ..ai_observability import cache_put, on_call_end, on_call_start
        payload = {"model": effective_model, "temperature": temperature,
                   "messages": messages}
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        obs_ctx = on_call_start(task_type=task_type, model=effective_model,
                                input_payload=payload, cacheable=True)
        if obs_ctx["cached_result"] is not None:
            content, usage = obs_ctx["cached_result"]
            on_call_end(obs_ctx, success=True, cached=True,
                        task_id=self._task_id)
            self._task_calls += 1
            return content, usage
        try:
            # 125：HTTP 层重试走分类策略；ai_service.chat 内部还有一层简单重试，
            # 这里直接调 _post 做单次 HTTP，由 RetryExecutor 统一控制重试。
            def _single_post():
                data = self._svc._post(payload)
                try:
                    content = data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as e:
                    raise AIFatalError(
                        "模型服务返回格式无效：缺少 choices.message.content") from e
                return content, data.get("usage", {})

            content, usage = executor.run(_single_post)
            tokens = int(usage.get("total_tokens", 0) or 0)
            self._task_calls += 1
            self._task_tokens += tokens
            # 126：调用后只复查 token 上限（调用次数在调用前已检查，
            # 第 5 次合法调用不应被自己熔断）
            if self._task_tokens >= MAX_TOKENS_PER_TASK:
                self._check_task_limits(task_type)
            if obs_ctx["cache_key"]:
                cache_put(obs_ctx["cache_key"], (content, usage))
            on_call_end(obs_ctx, success=True, usage=usage,
                        task_id=self._task_id)
            duration_ms = int((time.time() - start) * 1000)
            self._record_config(cfg, task_type, executor.attempts, tokens,
                                duration_ms, True)
            self._charge_budget(tokens)
            return content, usage
        except Exception as e:
            on_call_end(obs_ctx, success=False, error=str(e),
                        task_id=self._task_id)
            duration_ms = int((time.time() - start) * 1000)
            self._record_config(cfg, task_type, executor.attempts, tokens,
                                duration_ms, False, error=str(e))
            raise

    def generate_with_retry(
        self,
        task_type: str,
        make_messages: Callable[[Optional[str]], List[dict]],
        validate_fn: Callable[[dict], Tuple[List[str], object]],
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_attempts: int = 4,
    ) -> Tuple[object, dict]:
        """121：带校验重试的生成（替代 svc.generate_with_retry）。

        每次 chat 都走 gateway.chat（预算/重试/上限/配置记录全生效）。
        返回 (校验通过的对象, {attempts, usage})。行为与原接口一致。
        """
        from ..ai_service import parse_json_content, TOTAL_TIMEOUT
        import json as _json

        feedback: Optional[str] = None
        attempts = 0
        usage_list: List[dict] = []
        last_errors: List[str] = []
        start_ts = time.time()
        for n in range(1, max_attempts + 1):
            elapsed = time.time() - start_ts
            if elapsed > TOTAL_TIMEOUT:
                raise AIFatalError(
                    f"整体任务超时（{TOTAL_TIMEOUT}s，已用 {elapsed:.0f}s）："
                    "模型多次重试仍未返回合法结果，请稍后重试或简化输入。"
                )
            attempts = n
            messages = make_messages(feedback)
            content, usage = self.chat(task_type, messages, model=model,
                                       temperature=temperature, json_object=True)
            usage_list.append(usage)
            try:
                data = parse_json_content(content)
            except (ValueError, TypeError, _json.JSONDecodeError) as e:
                last_errors = [f"[解析] 模型输出不是合法 JSON: {e}"]
                feedback = "上次输出不是合法 JSON，请只输出纯 JSON，不要加任何解释文字或 markdown 标记。"
                continue
            errors, obj = validate_fn(data)
            if not errors:
                return obj, {"attempts": attempts, "usage": usage_list}
            last_errors = errors
            feedback = (
                "上次输出未通过校验，请修正后重新输出完整 JSON（只输出 JSON）：\n"
                + "\n".join(f"- {e}" for e in errors)
            )
        raise AIFatalError(
            f"已重试 {max_attempts - 1} 次仍未生成合法结果（共尝试 {attempts} 次）。"
            f"最后一次的校验问题：{'; '.join(last_errors[:5])}。"
            "建议：简化产品描述或缩短目标时长后重试。"
        )
