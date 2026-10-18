"""Phase 10（AI 网关扩展）自检。

- 127：Provider 适配器（请求构建/响应解析/错误分类）
- 128：任务选模型策略
- 129：模型能力注册表（调用前校验）
- 130：故障切换（主模型失败 → fallback 重试一次，记台账）
- 141-160：prompt 版本化（registry + 版本追溯）
- 评估框架：mock transport 跑固定用例集
"""
import os
import sys

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.ai_service import AIFatalError, AIRetryableError, DEFAULT_MODEL  # noqa: E402
from backend.ai.capabilities import (  # noqa: E402
    check_capability,
    get_capability,
    set_capability,
)
from backend.ai.evaluation import run_eval  # noqa: E402
from backend.ai.evaluation.runner import MockAdapter, validate_schema  # noqa: E402
from backend.ai.gateway import AIGateway  # noqa: E402
from backend.ai.model_policy import (  # noqa: E402
    get_policy,
    resolve_fallback,
    resolve_model,
    set_policy,
)
from backend.ai.prompts import get_prompt_version  # noqa: E402
from backend.ai.providers import DeepKeyAdapter, ProviderCapabilities  # noqa: E402
from backend.auth.database import get_session_factory, reset_engine  # noqa: E402
from backend.auth.models import Base  # noqa: E402,F401
from backend import models as m  # noqa: E402,F401


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """每个用例一个全新的 SQLite 文件库（alembic 建表到 0008）。"""
    url = f"sqlite:///{tmp_path}/t.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    reset_engine()
    from alembic.config import Config
    from alembic import command
    cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    command.upgrade(cfg, "head")
    session = get_session_factory()()
    yield session
    session.close()


class MockTransport:
    """按 model 名决定行为的 mock 传输。"""

    def __init__(self, fail_models=(), content='{"ok": true}'):
        self.fail_models = set(fail_models)
        self.content = content
        self.calls = []
        self.api_key = "mock-key"
        self.endpoint = "mock://x/v1/chat/completions"

    def _post(self, payload):
        self.calls.append(payload)
        if payload.get("model") in self.fail_models:
            raise AIRetryableError("mock 连接失败")
        return {
            "choices": [{"message": {"content": self.content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                      "total_tokens": 15},
        }


# ---------- 127 ----------

def test_adapter_build_payload():
    t = MockTransport()
    ad = DeepKeyAdapter(t)
    assert ad.name == "deepkey"
    p = ad.build_payload("m1", [{"role": "user", "content": "hi"}],
                         temperature=0.3, json_object=True)
    assert p["model"] == "m1"
    assert p["temperature"] == 0.3
    assert p["response_format"] == {"type": "json_object"}
    p2 = ad.build_payload("m1", [])
    assert "response_format" not in p2


def test_adapter_single_call_parses():
    t = MockTransport(content='{"ideas": []}')
    ad = DeepKeyAdapter(t)
    content, usage = ad.single_call(ad.build_payload("m", []))
    assert content == '{"ideas": []}'
    assert usage["total_tokens"] == 15


def test_adapter_single_call_bad_response():
    t = MockTransport(content="not json at all {{{")
    # single_call 只做结构解析，不校验 JSON 合法性（那是上层的事）
    ad = DeepKeyAdapter(t)
    content, _ = ad.single_call(ad.build_payload("m", []))
    assert content == "not json at all {{{"

    class BadTransport(MockTransport):
        def _post(self, payload):
            return {"no_choices": True}

    ad2 = DeepKeyAdapter(BadTransport())
    with pytest.raises(AIFatalError):
        ad2.single_call(ad2.build_payload("m", []))


def test_adapter_classify_error():
    t = MockTransport()
    ad = DeepKeyAdapter(t)
    assert ad.classify_error(AIRetryableError("连接超时")) == "connection"
    assert ad.classify_error(AIFatalError("401 鉴权失败")) == "client_error"
    assert ad.classify_error(
        AIFatalError("模型输出不是合法 JSON")) == "structure"


def test_adapter_capabilities():
    caps = DeepKeyAdapter(MockTransport()).capabilities()
    assert isinstance(caps, ProviderCapabilities)
    assert caps.text is True
    assert caps.structured_output is True


# ---------- 128 ----------

def test_policy_seed_defaults(db):
    # 种子：全部任务默认 DEFAULT_MODEL，保证现有行为不变
    for tt in ("ideas", "storyboard", "regen_shot", "feasibility",
               "risk_check"):
        pol = get_policy(db, tt)
        assert pol is not None, tt
        assert pol["model"] == DEFAULT_MODEL
    assert resolve_model(db, "ideas") == DEFAULT_MODEL
    assert resolve_model(db, "unknown_task") == DEFAULT_MODEL


def test_policy_explicit_wins(db):
    assert resolve_model(db, "ideas", explicit_model="custom-m") == "custom-m"


def test_policy_db_none():
    assert resolve_model(None, "ideas") == DEFAULT_MODEL
    assert resolve_fallback(None, "ideas") is None


def test_set_policy(db):
    set_policy(db, "ideas", "strong-m", "fallback-m")
    assert resolve_model(db, "ideas") == "strong-m"
    assert resolve_fallback(db, "ideas") == "fallback-m"
    with pytest.raises(ValueError):
        set_policy(db, "nope", "m")


# ---------- 129 ----------

def test_capability_match_passes(db):
    # 种子 gpt-6-luna supports_json=True
    check_capability(db, DEFAULT_MODEL, "ideas", json_object=True)


def test_capability_mismatch_raises(db):
    set_capability(db, "weak-m", supports_json=False)
    with pytest.raises(AIFatalError) as e:
        check_capability(db, "weak-m", "ideas", json_object=True)
    assert "不满足" in str(e.value)


def test_capability_db_none_skips():
    check_capability(None, "any", "ideas", json_object=True)  # 不抛错


def test_capability_unknown_model_skips(db):
    check_capability(db, "never-registered", "ideas")  # 无注册信息不拦截


# ---------- 130 ----------

def test_failover_primary_fails_fallback_succeeds(db):
    set_policy(db, "ideas", "bad-model", "good-model")
    t = MockTransport(fail_models={"bad-model"}, content='{"ideas": []}')
    gw = AIGateway(service=t, db=db, adapter=DeepKeyAdapter(t))
    content, usage = gw.chat("ideas", [{"role": "user", "content": "hi"}],
                             json_object=True)
    assert content == '{"ideas": []}'
    models_called = [c["model"] for c in t.calls]
    assert "bad-model" in models_called
    assert "good-model" in models_called


def test_no_failover_when_explicit_model(db):
    set_policy(db, "ideas", "bad-model", "good-model")
    t = MockTransport(fail_models={"bad-model", "good-model"})
    gw = AIGateway(service=t, db=db, adapter=DeepKeyAdapter(t))
    # 用户显式指定模型：失败不切换（122 禁止静默回退），直接抛错
    with pytest.raises(Exception):
        gw.chat("ideas", [{"role": "user", "content": "hi"}],
                model="bad-model")
    assert all(c["model"] == "bad-model" for c in t.calls)


def test_no_failover_without_fallback(db):
    t = MockTransport(fail_models={DEFAULT_MODEL})
    gw = AIGateway(service=t, db=db, adapter=DeepKeyAdapter(t))
    with pytest.raises(Exception):
        gw.chat("ideas", [{"role": "user", "content": "hi"}])
    # 未配 fallback：只调了主模型（重试次数由 RetryExecutor 控制 ≥1）
    assert all(c["model"] == DEFAULT_MODEL for c in t.calls)


# ---------- 141-160 ----------

def test_prompt_versions():
    assert get_prompt_version("ideas") == "ideas_v1"
    assert get_prompt_version("storyboard") == "storyboard_v1"
    assert get_prompt_version("regen_shot") == "regen_shot_v1"
    assert get_prompt_version("feasibility") == "feasibility_v1"
    assert get_prompt_version("risk_check") == "risk_review_v1"
    assert get_prompt_version("nope") == "unknown"


def test_prompt_version_recorded_to_task(db):
    from backend.models import AITask, Organization
    org = Organization(id="o1", name="t", created_by="u1")
    db.add(org)
    db.commit()
    task = AITask(id="t1", org_id="o1", created_by="u1", task_type="ideas",
                  status="running", payload_json={})
    db.add(task)
    db.commit()
    t = MockTransport(content='{"ideas": []}')
    gw = AIGateway(service=t, db=db, task_id="t1",
                   adapter=DeepKeyAdapter(t))
    gw.chat("ideas", [{"role": "user", "content": "hi"}],
            prompt_version="ideas_v1")
    db.refresh(task)
    assert task.prompt_version == "ideas_v1"
    assert task.model_used and DEFAULT_MODEL in task.model_used


# ---------- 评估框架 ----------

def test_validate_schema():
    assert validate_schema({"ideas": []},
                           {"type": "object", "required": ["ideas"]}) == []
    errs = validate_schema({},
                           {"type": "object", "required": ["ideas"]})
    assert any("ideas" in e for e in errs)
    errs = validate_schema({"ideas": "not-array"},
                           {"type": "object",
                            "properties": {"ideas": {"type": "array"}}})
    assert errs


def _isolated_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/eval.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    reset_engine()
    # 评估用 mock 响应按 task_type 区分，但缓存键只看输入内容；
    # 跨测试清缓存，避免上一个测试的 mock 响应污染下一个。
    from backend import ai_observability as obs
    obs._cache.clear()


def test_run_eval_all_pass(tmp_path, monkeypatch):
    _isolated_db(tmp_path, monkeypatch)
    responses = {
        "ideas": '{"ideas": [{"name": "a"}]}',
        "storyboard": '{"shots": [], "target_duration_seconds": 30}',
        "risk_check": '{"flags": []}',
        "feasibility": '{"results": []}',
    }
    report = run_eval(responses=responses)
    assert report.total == 4
    assert report.json_valid_rate == 1.0
    assert report.schema_pass_rate == 1.0
    assert report.avg_tokens > 0
    d = report.to_dict()
    assert d["total"] == 4


def test_run_eval_bad_json(tmp_path, monkeypatch):
    _isolated_db(tmp_path, monkeypatch)
    responses = {"__default__": "这不是 JSON！！！"}
    report = run_eval(responses=responses)
    assert report.json_valid_rate == 0.0
    assert report.schema_pass_rate == 0.0


def test_run_eval_schema_fail(tmp_path, monkeypatch):
    _isolated_db(tmp_path, monkeypatch)
    # JSON 合法但缺必填字段 → json_valid 通过，schema 不通过
    responses = {"__default__": '{"wrong": 1}'}
    report = run_eval(responses=responses)
    assert report.json_valid_rate == 1.0
    assert report.schema_pass_rate == 0.0
