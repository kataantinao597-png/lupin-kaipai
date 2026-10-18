"""评估执行器：跑固定用例集，输出质量报告。

- 默认用 MockAdapter（不需真实 Key），自检/CI 用。
- 传真实 transport 可跑真实模型评估（需 Key，运维手动执行）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ...ai_service import parse_json_content
from ..gateway import AIGateway
from ..providers.base import ProviderAdapter, ProviderCapabilities
from .cases import EvalCase, FIXED_CASES

logger = logging.getLogger(__name__)


# ---------- 最小 JSON schema 校验（只支持评估用到的子集） ----------

def validate_schema(data: Any, schema: dict) -> List[str]:
    """返回错误列表；空表示通过。支持 type/required/properties/items/enum。"""
    errors: List[str] = []
    _check(data, schema, "$", errors)
    return errors


def _check(data: Any, schema: dict, path: str, errors: List[str]) -> None:
    t = schema.get("type")
    if t == "object":
        if not isinstance(data, dict):
            errors.append(f"{path}: 期望 object，实际 {type(data).__name__}")
            return
        for key in schema.get("required", []):
            if key not in data:
                errors.append(f"{path}: 缺少必填字段 {key}")
        for key, subschema in schema.get("properties", {}).items():
            if key in data:
                _check(data[key], subschema, f"{path}.{key}", errors)
    elif t == "array":
        if not isinstance(data, list):
            errors.append(f"{path}: 期望 array，实际 {type(data).__name__}")
            return
        items_schema = schema.get("items")
        if items_schema:
            for i, item in enumerate(data[:5]):  # 只抽查前 5 个
                _check(item, items_schema, f"{path}[{i}]", errors)
    elif t == "string":
        if not isinstance(data, str):
            errors.append(f"{path}: 期望 string，实际 {type(data).__name__}")
    elif t == "number":
        if not isinstance(data, (int, float)) or isinstance(data, bool):
            errors.append(f"{path}: 期望 number，实际 {type(data).__name__}")
    enum = schema.get("enum")
    if enum is not None and data not in enum:
        errors.append(f"{path}: 值 {data!r} 不在 {enum} 中")


# ---------- Mock transport ----------

class MockTransport:
    """mock 底层传输：按 task_type 返回预设响应。不发起任何网络请求。"""

    def __init__(self, responses: Dict[str, str]):
        """
        responses: task_type → 模型输出文本（应为 JSON 字符串）。
        缺省的 task_type 返回一个通用合法 JSON。
        """
        self.responses = responses
        self.calls: List[dict] = []
        self.api_key = "mock-key"
        self.endpoint = "mock://eval/chat/completions"

    def _post(self, payload: dict) -> dict:
        self.calls.append(payload)
        model = payload.get("model", "")
        # 从 payload 反查 task_type：mock 场景下调用方通过 model 区分
        content = self.responses.get(model) or self.responses.get(
            "__default__", '{"ok": true}')
        return {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                      "total_tokens": 150},
        }


class MockAdapter(ProviderAdapter):
    """评估用 mock adapter：能力全开。"""

    @property
    def name(self) -> str:
        return "mock"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(text=True, vision=True,
                                    structured_output=True, streaming=False,
                                    max_context_tokens=128000)

    def build_payload(self, model: str, messages: List[dict],
                      temperature: float = 0.5,
                      json_object: bool = False) -> Dict[str, Any]:
        return {"model": model, "messages": messages,
                "temperature": temperature,
                "json_object": json_object}

    def single_call(self, payload: Dict[str, Any]) -> Tuple[str, dict]:
        data = self._transport._post(payload)
        content = data["choices"][0]["message"]["content"]
        return content, data.get("usage", {})


# ---------- 报告 ----------

@dataclass
class EvalReport:
    total: int = 0
    json_valid: int = 0
    schema_pass: int = 0
    total_tokens: int = 0
    cases: List[dict] = field(default_factory=list)

    @property
    def json_valid_rate(self) -> float:
        return self.json_valid / self.total if self.total else 0.0

    @property
    def schema_pass_rate(self) -> float:
        return self.schema_pass / self.total if self.total else 0.0

    @property
    def avg_tokens(self) -> float:
        return self.total_tokens / self.total if self.total else 0.0

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "json_valid_rate": round(self.json_valid_rate, 3),
            "schema_pass_rate": round(self.schema_pass_rate, 3),
            "avg_tokens": round(self.avg_tokens, 1),
            "cases": self.cases,
        }


def run_eval(cases: Optional[List[EvalCase]] = None,
             responses: Optional[Dict[str, str]] = None,
             transport=None) -> EvalReport:
    """跑评估。

    cases: 用例集，默认 FIXED_CASES。
    responses: mock 响应 {task_type: 输出文本}；transport 给定时忽略。
    transport: 真实 AIService（需 Key）或 MockTransport。
    """
    cases = cases or FIXED_CASES
    report = EvalReport(total=len(cases))

    if transport is None:
        transport = MockTransport(responses or {})

    for case in cases:
        # mock 按 task_type 取响应：把 task_type 编进 model 名
        adapter = MockAdapter(transport)
        gw = AIGateway(service=transport, db=None, adapter=adapter)

        result: dict = {"name": case.name, "task_type": case.task_type,
                        "prompt_version": case.prompt_version}
        try:
            # 直调单次 chat（不用 generate_with_retry 的校验重试），
            # 分别判定 JSON 合法性与 schema 通过率
            content, usage = gw.chat(
                case.task_type, case.messages, model=case.task_type,
                temperature=0.0, json_object=True,
                prompt_version=case.prompt_version or None)
            tokens = int(usage.get("total_tokens", 0) or 0)
            result["tokens"] = tokens
            report.total_tokens += tokens
            try:
                data = parse_json_content(content)
                result["json_valid"] = True
                report.json_valid += 1
            except Exception as e:
                result["json_valid"] = False
                result["schema_pass"] = False
                result["error"] = f"JSON 非法: {e}"[:300]
                report.cases.append(result)
                continue
            schema_errors = validate_schema(data, case.expected_schema)
            result["schema_pass"] = not schema_errors
            result["schema_errors"] = schema_errors[:5]
            if result["schema_pass"]:
                report.schema_pass += 1
        except Exception as e:
            result["json_valid"] = False
            result["schema_pass"] = False
            result["error"] = str(e)[:300]
        report.cases.append(result)
    return report
