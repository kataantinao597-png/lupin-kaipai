"""Phase 5（131-138）AI 可观测性组自检。

- 131：调用记录 token 正确（后台线程写库）
- 132：成本估算正确
- 133/134：台账分页过滤、用量汇总
- 135：熔断触发 / 半开恢复
- 136：并发超限排队超时
- 137：优先级 high 先于 low 执行
- 138：相同输入缓存命中；敏感输入不缓存
"""
import os
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.auth.database import (  # noqa: E402
    get_session_factory,
    reset_engine,
)
from backend.auth.models import Base  # noqa: E402,F401
from backend import ai_observability as obs  # noqa: E402
from backend.ai_service import AIRetryableError, AIFatalError  # noqa: E402
from backend.models import AICall  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """每个用例一个全新的 SQLite 文件库（alembic 建表）。"""
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
    reset_engine()


@pytest.fixture(autouse=True)
def _clean_state():
    """隔离内存状态：熔断/信号量/缓存/价格缓存。"""
    obs._breakers.clear()
    obs._semaphores.clear()
    obs._cache.clear()
    obs._price_cache.clear()
    yield
    obs._breakers.clear()
    obs._semaphores.clear()
    obs._cache.clear()
    obs._price_cache.clear()


def _wait_for(pred, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


# ---------- 132：成本估算 ----------

def test_estimate_cost_cents(db):
    # 种子价 gpt-6-luna：input 0.010/1k，output 0.030/1k（CNY）
    cents, version = obs.estimate_cost_cents("gpt-6-luna", 2000, 1000)
    assert cents == 5, cents  # (2*0.01 + 1*0.03)*100
    assert version >= 1
    cents0, _ = obs.estimate_cost_cents("gpt-6-luna", 0, 0)
    assert cents0 == 0


# ---------- 131：调用记录 ----------

def test_record_success(db):
    usage = {"prompt_tokens": 2000, "completion_tokens": 1000,
             "total_tokens": 3000}
    content, ret_usage = obs.run_observed(
        task_type="ideas", model="gpt-6-luna",
        payload={"model": "gpt-6-luna",
                 "messages": [{"role": "user", "content": "hello"}]},
        do_call=lambda: ("content-ok", usage),
        cacheable=False,
    )
    assert content == "content-ok" and ret_usage == usage
    assert _wait_for(
        lambda: db.query(AICall).filter_by(task_type="ideas").count() >= 1)
    rec = db.query(AICall).filter_by(task_type="ideas").one()
    assert rec.model == "gpt-6-luna"
    assert rec.prompt_tokens == 2000
    assert rec.completion_tokens == 1000
    assert rec.total_tokens == 3000
    assert rec.cost_cents == 5
    assert rec.status == "success"
    assert rec.latency_ms is not None and rec.latency_ms >= 0


def test_record_failed(db):
    def _boom():
        raise AIRetryableError("fake down")

    with pytest.raises(AIRetryableError):
        obs.run_observed(task_type="feasibility", model="gpt-6-luna",
                         payload={"x": 1}, do_call=_boom, cacheable=False)
    assert _wait_for(
        lambda: db.query(AICall).filter_by(status="failed").count() >= 1)
    rec = db.query(AICall).filter_by(status="failed").one()
    assert rec.task_type == "feasibility"
    assert "fake down" in (rec.error or "")


# ---------- 133/134：台账与汇总 ----------

def test_calls_api_and_summary(db):
    """133/134：台账与汇总。

    2026-10-19 起两个接口按组织隔离（`ai_calls` 无 org_id，经 task_id → ai_tasks.org_id
    归属），因此这里先建组织和任务，并让调用挂到该任务上；未挂任务的调用不计入。
    """
    from backend.api.v1.ai_stats import list_calls, usage_summary
    from backend import models

    org = models.Organization(id="org-t1", name="测试组织", created_by="u-t1")
    db.add(org)
    db.commit()  # ai_tasks.org_id 是硬外键，且两模型间无 relationship，
    # 一并 add_all 时 unit of work 不保证 organizations 先插 → 必须分两次提交
    task = models.AITask(id="task-t1", org_id="org-t1", created_by="u-t1",
                         task_type="ideas", payload_json={})
    db.add(task)
    db.commit()

    # 无 task_id 的调用无法归属组织，必须被排除在统计之外
    obs.run_observed(task_type="risk_check", model="gpt-5.6-luna",
                     payload={"k": "orphan"}, do_call=lambda: ("c", {"prompt_tokens": 9,
                     "completion_tokens": 9, "total_tokens": 18}),
                     cacheable=False)

    usage = {"prompt_tokens": 100, "completion_tokens": 50,
             "total_tokens": 150}
    obs.run_observed(task_type="ideas", model="gpt-6-luna",
                     payload={"k": "a"}, do_call=lambda: ("c", usage),
                     cacheable=False, task_id="task-t1")
    obs.run_observed(task_type="risk_check", model="gpt-5.6-luna",
                     payload={"k": "b"}, do_call=lambda: ("c", usage),
                     cacheable=False, task_id="task-t1")
    assert _wait_for(lambda: db.query(AICall).count() >= 3)
    db.expire_all()

    kw = dict(task_type=None, model=None, start=None, end=None,
              org_id="org-t1", db=db)
    page1 = list_calls(page=1, page_size=1, **kw)
    assert page1["total"] == 2 and len(page1["items"]) == 1
    assert page1["org_id"] == "org-t1"
    only_ideas = list_calls(page=1, page_size=20,
                            **{**kw, "task_type": "ideas"})
    assert only_ideas["total"] == 1
    only_model = list_calls(page=1, page_size=20,
                            **{**kw, "model": "gpt-5.6-luna"})
    assert only_model["total"] == 1

    summary = usage_summary(days=30, org_id="org-t1", db=db)
    assert summary["today"]["tokens"] == 300
    assert summary["today"]["calls"] == 2
    assert summary["month"]["tokens"] == 300
    names = {m["name"] for m in summary["by_model"]}
    assert {"gpt-6-luna", "gpt-5.6-luna"} <= names
    ttypes = {t["name"] for t in summary["by_task_type"]}
    assert {"ideas", "risk_check"} <= ttypes


def test_ai_usage_requires_org_admin(db):
    """2026-10-19 权限收紧：必须登录 + 是本组织 owner/admin。"""
    from backend.api.v1.ai_stats import _org_admin
    from backend.auth.models import User
    from backend.core.exceptions import AuthError, BusinessError
    from backend import models

    org = models.Organization(id="org-p1", name="权限测试", created_by="u-p1")
    user = User(id="u-p1", email="p1@test.local", password_hash="x")
    db.add_all([org, user])
    db.commit()

    # 组织不存在 → 404 语义
    with pytest.raises(BusinessError):
        _org_admin(org_id="no-such-org", user=user, db=db)
    # 非成员 → 403
    with pytest.raises(AuthError):
        _org_admin(org_id="org-p1", user=user, db=db)

    ms = models.Membership(id="ms-p1", user_id="u-p1", org_id="org-p1", role="editor")
    db.add(ms)
    db.commit()
    # editor 不够
    with pytest.raises(AuthError):
        _org_admin(org_id="org-p1", user=user, db=db)

    # admin 通过
    ms.role = "admin"
    db.commit()
    assert _org_admin(org_id="org-p1", user=user, db=db) == "org-p1"



# ---------- 135：熔断 ----------

def test_breaker_trip_and_recover():
    model = "test-breaker-x"
    for _ in range(5):
        obs.breaker_failure(model)
    st = obs.breaker_status(model)
    assert st["open"] is True
    with pytest.raises(AIFatalError, match="熔断中"):
        obs.breaker_check(model)
    # 熔断期间 run_observed 直接失败，不调用 do_call
    called = []
    with pytest.raises(AIFatalError, match="熔断中"):
        obs.run_observed(task_type="t", model=model, payload={},
                         do_call=lambda: (called.append(1), {}),
                         cacheable=False)
    assert called == []
    # 半开：窗口过期后允许试探
    obs._breaker_state(model)["opened_at"] = time.time() - 400
    obs.breaker_check(model)  # 不抛
    obs.breaker_success(model)
    assert obs.breaker_status(model)["failures"] == 0
    obs.breaker_check(model)  # 已恢复，不抛


def test_breaker_counts_via_run_observed():
    model = "test-breaker-y"
    for _ in range(4):
        try:
            obs.run_observed(task_type="t", model=model, payload={"i": 1},
                             do_call=lambda: (_ for _ in ()).throw(
                                 AIRetryableError("down")),
                             cacheable=False)
        except AIRetryableError:
            pass
    assert obs.breaker_status(model)["open"] is False
    try:
        obs.run_observed(task_type="t", model=model, payload={"i": 1},
                         do_call=lambda: (_ for _ in ()).throw(
                             AIRetryableError("down")),
                         cacheable=False)
    except AIRetryableError:
        pass
    assert obs.breaker_status(model)["open"] is True


# ---------- 136：并发控制 ----------

def test_concurrency_timeout(monkeypatch):
    monkeypatch.setattr(obs, "ACQUIRE_TIMEOUT", 1)
    model = "test-conc"
    obs.set_concurrency_limit(model, 1)
    sem = obs._semaphore_for(model)
    assert sem.acquire(blocking=False)
    try:
        with pytest.raises(AIRetryableError, match="并发超限"):
            obs.on_call_start(task_type="t", model=model,
                              input_payload={"a": 1}, cacheable=False)
    finally:
        sem.release()
    # 释放后可正常进入
    ctx = obs.on_call_start(task_type="t", model=model,
                            input_payload={"a": 1}, cacheable=False)
    obs.on_call_end(ctx, success=True, usage={"total_tokens": 1})


# ---------- 137：优先级 ----------

def test_priority_high_before_low(monkeypatch):
    from backend.ai_tasks.queue import _PriorityDispatcher
    order = []
    started = threading.Event()
    release = threading.Event()

    def fake_execute(self, task_id, runner, model_used, session_factory):
        order.append(task_id)
        if task_id == "blocker":
            started.set()
            release.wait(timeout=10)

    monkeypatch.setattr(_PriorityDispatcher, "_execute", fake_execute)
    disp = _PriorityDispatcher(max_workers=1)
    disp.submit("blocker", lambda t, c: {}, None, lambda: None,
                priority="low")
    assert started.wait(timeout=5)
    disp.submit("low1", lambda t, c: {}, None, lambda: None, priority="low")
    disp.submit("high1", lambda t, c: {}, None, lambda: None, priority="high")
    release.set()
    assert _wait_for(lambda: len(order) >= 3)
    assert order == ["blocker", "high1", "low1"], order


# ---------- 138：缓存 ----------

def test_cache_hit():
    model = "test-cache-m"
    payload = {"model": model,
               "messages": [{"role": "user", "content": "hi"}]}
    calls = []

    def _do():
        calls.append(1)
        return ("cached-content",
                {"prompt_tokens": 5, "completion_tokens": 5,
                 "total_tokens": 10})

    r1 = obs.run_observed(task_type="ideas", model=model, payload=payload,
                          do_call=_do)
    r2 = obs.run_observed(task_type="ideas", model=model, payload=payload,
                          do_call=_do)
    assert r1 == r2 == ("cached-content", {"prompt_tokens": 5,
                                           "completion_tokens": 5,
                                           "total_tokens": 10})
    assert len(calls) == 1  # 第二次命中缓存，未真实调用
    # 不同输入不命中
    payload2 = {"model": model,
                "messages": [{"role": "user", "content": "bye"}]}
    obs.run_observed(task_type="ideas", model=model, payload=payload2,
                     do_call=_do)
    assert len(calls) == 2


def test_cache_skips_sensitive():
    model = "test-cache-s"
    payload = {"model": model, "api_key": "sk-secret",
               "messages": [{"role": "user", "content": "hi"}]}
    calls = []

    def _do():
        calls.append(1)
        return ("c", {"total_tokens": 1})

    obs.run_observed(task_type="ideas", model=model, payload=payload,
                     do_call=_do)
    obs.run_observed(task_type="ideas", model=model, payload=payload,
                     do_call=_do)
    assert len(calls) == 2  # 敏感输入不缓存


def test_cache_respects_cacheable_false():
    model = "test-cache-nc"
    payload = {"model": model, "messages": [{"role": "user", "content": "x"}]}
    calls = []

    def _do():
        calls.append(1)
        return ("c", {"total_tokens": 1})

    for _ in range(2):
        obs.run_observed(task_type="ideas", model=model, payload=payload,
                         do_call=_do, cacheable=False)
    assert len(calls) == 2
