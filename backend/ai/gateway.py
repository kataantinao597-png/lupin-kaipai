"""121：统一 AI Gateway。

所有业务（创意/分镜/风险/可行性）都走 gateway，不再直接调 ai_service。
ai_service.AIService 保留为底层传输层（HTTP 调用 + 错误映射）。

网关职责：
- 121：统一入口 chat() / generate_with_retry()
- 122：记录实际生效的 provider/model/config_version（日志 + ai_tasks.model_used）
- 124：调用前检查组织预算，超限返回 402
- 125：HTTP 层重试走 backend/ai/retry.py 的分类策略
- 126：单任务最多 5 次调用、最多 500K tokens，超限熔断并告警
- 127：经 ProviderAdapter 调用，不再直拼 URL / 直接解析响应
- 128：model=None 时按 task_model_policy 选模型
- 129：调用前校验模型能力，不满足直接报错不浪费调用
- 130：主模型失败且有 fallback 时自动切换重试一次（预算内），记 failover 台账
- 141-160：prompt_version 透传并记录到 ai_tasks.prompt_version
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
from .capabilities import check_capability
from .config import ModelConfig, build_config
from .model_policy import resolve_fallback, resolve_model
from .providers import DeepKeyAdapter, ProviderAdapter
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
                 alert_fn: Optional[Callable[[str, dict], None]] = None,
                 adapter: Optional[ProviderAdapter] = None):
        """
        service: ai_service.AIService 实例（传输层）
        org_id: 组织 ID（124 预算检查用；None 则跳过预算）
        db: SQLAlchemy session（预算扣减 / ai_tasks.model_used 回写 /
            128 策略 / 129 能力查表用；None 则只记日志并用默认策略）
        task_id: 关联的 ai_tasks.id（122 回写 model_used 用）
        alert_fn: 126 熔断告警回调 fn(message, context)
        adapter: 127 Provider 适配器；None 则用 DeepKeyAdapter(service)
        """
        self._svc = service
        self._org_id = org_id
        self._db = db
        self._task_id = task_id
        self._alert_fn = alert_fn or (lambda m, c: logger.warning("AI告警: %s %s", m, c))
        self._adapter = adapter or DeepKeyAdapter(service)
        # 126：本网关实例内的任务级计数
        self._task_calls = 0
        self._task_tokens = 0

    def bind_org(self, org_id: Optional[str]) -> None:
        """把本次调用绑定到已校验的组织（P0 修复用）。

        `get_ai_gateway` 只从 `X-Org-Id` 头与 `?org_id=` 取组织；当 org_id 只出现在
        请求体里时，路由层校验通过后用它把两边对齐，避免「校验的是 A 组织、
        记账记到无组织」这种口径分裂。
        """
        if org_id:
            self._org_id = org_id

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

    def _notify_webhook(self, event_type: str, payload: dict) -> None:
        """S10：best-effort 投递组织事件；失败只记日志，不影响主流程。"""
        if not self._org_id:
            return
        try:
            from ..services.webhook_service import dispatch_event
            dispatch_event(event_type, self._org_id, payload)
        except Exception:  # noqa: BLE001
            logger.warning("webhook 事件投递失败 event=%s", event_type,
                           exc_info=True)

    def _check_budget(self, task_type: str):
        """124：调用前检查组织预算。超限抛 BudgetExceededError（API 层转 402）。

        S10：预算超限时发布 budget.exceeded 事件；随后检查 ai_calls_daily
        配额（QuotaExceededError 为 BudgetExceededError 子类，同样走 402）。
        """
        if not self._org_id or self._db is None:
            return
        from .budget import check_budget
        from ..services.quota_service import QuotaExceededError
        try:
            check_budget(self._db, self._org_id, task_type)
        except QuotaExceededError:
            raise
        except BudgetExceededError as e:
            self._notify_webhook("budget.exceeded", {
                "task_type": task_type, "error": str(e)[:200]})
            raise
        # S10：每日 AI 调用配额
        from ..services.quota_service import check_quota
        check_quota(self._db, self._org_id, "ai_calls_daily")

    def _record_config(self, cfg: ModelConfig, task_type: str, attempts: int,
                       tokens: int, duration_ms: int, success: bool,
                       error: str = "", prompt_version: Optional[str] = None):
        """122：记录实际生效配置到日志；有 db+task_id 时回写 ai_tasks。
        141-160：同时回写 prompt_version。"""
        rec = GatewayCallRecord(
            task_type=task_type, provider=cfg.provider, model=cfg.model,
            config_version=cfg.config_version, requested_model=cfg.requested_model,
            attempts=attempts, total_tokens=tokens, duration_ms=duration_ms,
            success=success, error=error[:200],
        )
        logger.info("AI调用 task=%s provider=%s model=%s cfg=%s prompt=%s tokens=%d success=%s",
                    rec.task_type, rec.provider, rec.model, rec.config_version,
                    prompt_version or "-", rec.total_tokens, rec.success)
        if self._db is not None and self._task_id:
            try:
                from ..models import AITask
                task = self._db.get(AITask, self._task_id)
                if task is not None:
                    task.model_used = f"{cfg.provider}/{cfg.model}@{cfg.config_version}"
                    if prompt_version and hasattr(task, "prompt_version"):
                        task.prompt_version = prompt_version
                    self._db.commit()
            except Exception as e:
                logger.warning("回写 ai_tasks.model_used 失败: %s", e)

    def _record_failover(self, task_type: str, primary: str, fallback: str,
                         error: str):
        """130：故障切换记入 ai_calls 台账（status=failover），便于审计。"""
        try:
            from ..ai_observability import record_call_async
            from ..ai_observability import _utcnow
            record_call_async({
                "task_id": self._task_id, "org_id": self._org_id,
                "task_type": task_type,
                "model": fallback, "prompt_tokens": 0, "completion_tokens": 0,
                "total_tokens": 0, "cost_cents": 0, "status": "failover",
                "error": f"主模型 {primary} 失败后切换: {error[:300]}",
                "latency_ms": 0, "cached": False,
                "created_at": _utcnow(),
            })
        except Exception as e:
            logger.warning("记录 failover 台账失败: %s", e)
        logger.warning("AI故障切换 task=%s 主模型=%s → 兜底=%s 原因=%s",
                       task_type, primary, fallback, error[:200])

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
             json_object: bool = False,
             prompt_version: Optional[str] = None) -> Tuple[str, dict]:
        """121：单次 chat 调用。禁止静默回退：指定模型即用该模型。

        model=None → 128 按 task_type 查策略表；再无则 DEFAULT_MODEL。
        prompt_version：141-160，记录到 ai_tasks.prompt_version。
        返回 (content, usage)。usage 为模型返回的 usage 字典。
        """
        # 128：解析实际使用的模型（显式指定优先）
        primary = resolve_model(self._db, task_type, model)
        # 129：调用前能力校验（不满足直接报错，不浪费调用）
        check_capability(self._db, primary, task_type, json_object=json_object)
        try:
            return self._chat_once(task_type, messages, model=primary,
                                   temperature=temperature,
                                   json_object=json_object,
                                   prompt_version=prompt_version,
                                   requested_model=model)
        except Exception as e:
            # 130：故障切换 —— 仅当模型来自策略（非用户显式指定）且配了 fallback
            if model is not None:
                raise  # 用户显式指定的模型，禁止静默切换（122）
            fallback = resolve_fallback(self._db, task_type)
            if not fallback or fallback == primary:
                raise
            self._record_failover(task_type, primary, fallback, str(e))
            # 兜底调用同样受预算/任务上限约束
            return self._chat_once(task_type, messages, model=fallback,
                                   temperature=temperature,
                                   json_object=json_object,
                                   prompt_version=prompt_version,
                                   requested_model=None)

    def _chat_once(self, task_type: str, messages: List[dict],
                   model: str,
                   temperature: float = 0.5,
                   json_object: bool = False,
                   prompt_version: Optional[str] = None,
                   requested_model: Optional[str] = None) -> Tuple[str, dict]:
        """单次实际调用（预算/重试/上限/配置记录/可观测性全生效）。"""
        # 126：先检查任务级上限
        self._check_task_limits(task_type)
        # 124：再检查组织预算
        self._check_budget(task_type)
        # 122：配置快照（实际生效值）；requested_model 保留原始用户指定
        cfg = self._model_config(requested_model=requested_model or model)
        # 122：禁止静默回退 —— 用解析后的模型
        effective_model = model or cfg.model

        start = time.time()
        executor = RetryExecutor()
        # S9.3：重试/超时指标埋点
        def _on_ai_attempt(attempt_no: int, category: str) -> None:
            try:
                from ..core import metrics as _metrics
                _metrics.record_ai_retry(category)
                if category in ("timeout", "connection"):
                    _metrics.record_ai_timeout()
            except Exception:
                pass
        executor.on_attempt = _on_ai_attempt
        tokens = 0
        # Phase 5 可观测性钩子：网关调用前后通知 observability 记录/熔断/并发/缓存
        from ..ai_observability import cache_put, on_call_end, on_call_start
        # 127：经 ProviderAdapter 构建请求（不再直拼 payload）
        payload = self._adapter.build_payload(effective_model, messages,
                                              temperature, json_object)
        obs_ctx = on_call_start(task_type=task_type, model=effective_model,
                                input_payload=payload, cacheable=True)
        if obs_ctx["cached_result"] is not None:
            content, usage = obs_ctx["cached_result"]
            on_call_end(obs_ctx, success=True, cached=True,
                        task_id=self._task_id, org_id=self._org_id)
            self._task_calls += 1
            return content, usage
        try:
            # 127：经 ProviderAdapter 单次调用 + 响应解析
            def _single_post():
                return self._adapter.single_call(payload)

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
                        task_id=self._task_id, org_id=self._org_id)
            duration_ms = int((time.time() - start) * 1000)
            self._record_config(cfg, task_type, executor.attempts, tokens,
                                duration_ms, True, prompt_version=prompt_version)
            self._charge_budget(tokens)
            return content, usage
        except Exception as e:
            on_call_end(obs_ctx, success=False, error=str(e),
                        task_id=self._task_id, org_id=self._org_id)
            duration_ms = int((time.time() - start) * 1000)
            self._record_config(cfg, task_type, executor.attempts, tokens,
                                duration_ms, False, error=str(e),
                                prompt_version=prompt_version)
            raise

    def generate_with_retry(
        self,
        task_type: str,
        make_messages: Callable[[Optional[str]], List[dict]],
        validate_fn: Callable[[dict], Tuple[List[str], object]],
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_attempts: int = 4,
        prompt_version: Optional[str] = None,
    ) -> Tuple[object, dict]:
        """121：带校验重试的生成（替代 svc.generate_with_retry）。

        每次 chat 都走 gateway.chat（预算/重试/上限/配置记录全生效）。
        prompt_version 透传并记录到 ai_tasks。
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
                                       temperature=temperature, json_object=True,
                                       prompt_version=prompt_version)
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
