"""V3.1 P1：店播场控脚本体验升级回归测试。

- sweep_timed_out：超时任务标 failed / 未超时不动 / 终态不动
- _validate_livestream：pending_confirms 可选字段校验
- _validate_regen_segment：单段重生成校验（时间轴一致/话术非空/phase 合法）
- run_livestream_script：元信息注入（schema_version/prompt_version/时间戳）
- run_livestream_regen_segment：只返回单段 + prompt_version
- POST /api/v1/ai-tasks：新任务类型 livestream_regen_segment 可建任务
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import backend.auth  # noqa: F401,E402  先 import，保证包初始化顺序


def _valid_script():
    return {
        "title": "樱桃专场",
        "total_minutes": 60,
        "segments": [
            {"phase": "开场留人", "start_minute": 0, "end_minute": 5,
             "goal": "留人", "script": ["欢迎来到直播间"], "tips": []},
            {"phase": "讲品", "start_minute": 5, "end_minute": 60,
             "goal": "卖货", "script": ["樱桃脆甜"], "tips": ["上福袋"]},
        ],
    }


class TestTimeoutSweep:
    def _seed(self, status, age_seconds, org_id):
        from backend.auth.database import get_session_factory
        from backend.ai_tasks.repository import AITaskRepository
        from backend.models import AITask
        s = get_session_factory()()
        try:
            t = AITaskRepository(s).create(org_id=org_id,
                                          task_type="livestream_script",
                                          payload={}, created_by="u1")
            tid = t.id
            task = s.get(AITask, tid)
            task.status = status
            task.created_at = (datetime.now(timezone.utc)
                               - timedelta(seconds=age_seconds))
            s.commit()
            return tid
        finally:
            s.close()

    def _status_of(self, tid):
        from backend.auth.database import get_session_factory
        from backend.models import AITask
        s = get_session_factory()()
        try:
            t = s.get(AITask, tid)
            return t.status, t.error
        finally:
            s.close()

    def test_old_pending_marked_cancelled(self, api_env):
        # 状态机只允许 pending→cancelled：排队超时按取消落库
        from backend.ai_tasks.queue import (sweep_timed_out,
                                            TASK_TIMEOUT_SECONDS)
        from backend.auth.database import get_session_factory
        tid = self._seed("pending", TASK_TIMEOUT_SECONDS + 60, api_env.org1)
        n = sweep_timed_out(get_session_factory())
        assert n == 1
        st, err = self._status_of(tid)
        assert st == "cancelled"
        assert "超时" in (err or "")

    def test_old_running_marked_failed(self, api_env):
        from backend.ai_tasks.queue import (sweep_timed_out,
                                            TASK_TIMEOUT_SECONDS)
        from backend.auth.database import get_session_factory
        tid = self._seed("running", TASK_TIMEOUT_SECONDS + 60, api_env.org1)
        n = sweep_timed_out(get_session_factory())
        assert n == 1
        st, err = self._status_of(tid)
        assert st == "failed"
        assert "超时" in (err or "")

    def test_young_running_untouched(self, api_env):
        from backend.ai_tasks.queue import sweep_timed_out
        from backend.auth.database import get_session_factory
        tid = self._seed("running", 60, api_env.org1)
        n = sweep_timed_out(get_session_factory())
        assert n == 0
        st, _ = self._status_of(tid)
        assert st == "running"

    def test_terminal_untouched(self, api_env):
        from backend.ai_tasks.queue import sweep_timed_out
        from backend.auth.database import get_session_factory
        tid = self._seed("succeeded", 3600 * 5, api_env.org1)
        n = sweep_timed_out(get_session_factory())
        assert n == 0
        st, _ = self._status_of(tid)
        assert st == "succeeded"

    def test_sweep_idempotent(self, api_env):
        from backend.ai_tasks.queue import sweep_timed_out, TASK_TIMEOUT_SECONDS
        from backend.auth.database import get_session_factory
        self._seed("pending", TASK_TIMEOUT_SECONDS + 60, api_env.org1)
        assert sweep_timed_out(get_session_factory()) == 1
        # 第二次：已是终态，不再处理
        assert sweep_timed_out(get_session_factory()) == 0


class TestPendingConfirms:
    def test_valid_list_ok(self):
        from backend.ai_tasks.runners import _validate_livestream
        d = _valid_script()
        d["pending_confirms"] = ["价格：用【价格待确认】占位"]
        errs, _ = _validate_livestream(d, 60)
        assert errs == []

    def test_absent_ok(self):
        from backend.ai_tasks.runners import _validate_livestream
        errs, _ = _validate_livestream(_valid_script(), 60)
        assert errs == []

    def test_not_list_rejected(self):
        from backend.ai_tasks.runners import _validate_livestream
        d = _valid_script()
        d["pending_confirms"] = "价格待确认"
        errs, out = _validate_livestream(d, 60)
        assert errs and out is None

    def test_non_string_item_rejected(self):
        from backend.ai_tasks.runners import _validate_livestream
        d = _valid_script()
        d["pending_confirms"] = ["价格待确认", 123]
        errs, out = _validate_livestream(d, 60)
        assert errs and out is None


class TestRegenSegmentValidator:
    def _seg(self):
        return {"phase": "讲品", "start_minute": 5, "end_minute": 25,
                "goal": "卖货", "script": ["新话术1", "新话术2"],
                "tips": ["新提示"]}

    def test_valid(self):
        from backend.ai_tasks.runners import _validate_regen_segment
        errs, out = _validate_regen_segment(self._seg(), 5, 25)
        assert errs == [] and out is not None

    def test_timeline_changed_rejected(self):
        from backend.ai_tasks.runners import _validate_regen_segment
        s = self._seg()
        s["end_minute"] = 30
        errs, out = _validate_regen_segment(s, 5, 25)
        assert errs and out is None
        assert any("时间轴" in e for e in errs)

    def test_empty_script_rejected(self):
        from backend.ai_tasks.runners import _validate_regen_segment
        s = self._seg()
        s["script"] = []
        errs, out = _validate_regen_segment(s, 5, 25)
        assert errs and out is None

    def test_bad_phase_rejected(self):
        from backend.ai_tasks.runners import _validate_regen_segment
        s = self._seg()
        s["phase"] = "乱入"
        errs, out = _validate_regen_segment(s, 5, 25)
        assert errs and out is None


class _FakeDB:
    def close(self):
        pass


class _FakeGateway:
    """最小网关替身：返回固定结果并跑校验器。"""

    def __init__(self, result):
        self.result = result

    def generate_with_retry(self, task_type, make_messages, validate_fn,
                            model=None, temperature=0.3, max_attempts=4,
                            prompt_version=None):
        data = self.result
        errs, out = validate_fn(data)
        assert not errs, "替身结果未通过校验: %s" % (errs,)
        return out, {"attempts": 1, "usage": {}}


class TestRegenRunner:
    def test_returns_single_segment(self, monkeypatch):
        import backend.ai_tasks.runners as R
        seg = {"phase": "讲品", "start_minute": 5, "end_minute": 25,
               "goal": "卖货", "script": ["新话术"], "tips": []}
        monkeypatch.setattr(
            R, "_gateway_for_runner",
            lambda task_id, payload, service=None: (_FakeGateway(seg),
                                                    _FakeDB()))
        out = R.run_livestream_regen_segment(
            "t1", lambda: False,
            {"product": {"product_name": "樱桃"}, "facts": [],
             "category": "水果", "duration_minutes": 60,
             "segment_index": 2,
             "segment": {"phase": "讲品", "start_minute": 5,
                         "end_minute": 25, "goal": "卖货"}})
        assert out["segment"]["script"] == ["新话术"]
        assert out["segment_index"] == 2
        assert out["segment"]["prompt_version"] == "livestream_v2"
        # 只返回单段：没有 segments/livestream_script 键
        assert "segments" not in out and "livestream_script" not in out

    def test_missing_timeline_fatal(self, monkeypatch):
        import backend.ai_tasks.runners as R
        from backend.ai_service import AIFatalError
        monkeypatch.setattr(
            R, "_gateway_for_runner",
            lambda task_id, payload, service=None: (_FakeGateway({}),
                                                    _FakeDB()))
        with pytest.raises(AIFatalError):
            R.run_livestream_regen_segment(
                "t1", lambda: False,
                {"product": {}, "segment": {"phase": "讲品"}})


class TestScriptMeta:
    def test_meta_injected(self, monkeypatch):
        import backend.ai_tasks.runners as R
        script = _valid_script()
        monkeypatch.setattr(
            R, "_gateway_for_runner",
            lambda task_id, payload, service=None: (_FakeGateway(script),
                                                    _FakeDB()))
        out = R.run_livestream_script(
            "t1", lambda: False,
            {"product": {"product_name": "樱桃"}, "facts": [],
             "category": "水果", "duration_minutes": 60,
             "extra": {"platform": "抖音"}})
        sc = out["livestream_script"]
        assert sc["schema_version"] == "livestream_script_v1"
        assert sc["prompt_version"] == "livestream_v2"
        assert sc["generated_at"] and sc["updated_at"]
        # 原有字段不受影响
        assert sc["title"] == "樱桃专场" and len(sc["segments"]) == 2


def _drain_task(client, task_id, timeout=30):
    """等任务落终态再结束测试。

    V3.1 P1 flaky 根因：dispatcher 工作线程是进程全局的；若测试提交任务后
    不等它跑完就 teardown（reset_engine），工作线程随后调用全局
    get_session_factory() 会复活一个绑定错误数据库的工厂，造成跨库污染、
    后续 fixture 出现 UNIQUE/无绑定等随机失败。排空后工作线程已退出，
    teardown 即安全。
    """
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = client.get("/api/v1/ai-tasks/%s" % task_id)
        if r.status_code != 200:
            return
        if r.json().get("status") in ("succeeded", "failed", "cancelled"):
            return
        time.sleep(0.5)
    raise AssertionError("任务 %s 未在 %ss 内落终态" % (task_id, timeout))


class TestRegenTaskTypeAPI:
    def test_create_regen_task(self, api_env):
        c = api_env.as_user("u1")
        r = c.post("/api/v1/ai-tasks", json={
            "task_type": "livestream_regen_segment",
            "org_id": api_env.org1,
            "payload": {
                "product": {"product_name": "樱桃"},
                "duration_minutes": 60,
                "segment_index": 1,
                "segment": {"phase": "讲品", "start_minute": 5,
                            "end_minute": 25, "goal": ""},
            }}, headers={"Idempotency-Key": "regen-key-1"})
        assert r.status_code == 202, r.text
        assert r.json()["task_type"] == "livestream_regen_segment"
        _drain_task(c, r.json()["id"])  # 无 AI 配置，任务会快速失败落终态


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    """独立 fixture（避免跨文件依赖）：单组织单用户。"""
    url = "sqlite:///%s/v31p1.db" % tmp_path
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    monkeypatch.delenv("LUPIN_API_KEY", raising=False)
    from backend.auth.database import reset_engine, get_session_factory, get_db
    from backend.auth.deps import get_current_user
    from backend.core.idempotency import reset_idempotency_store
    from backend.security.rate_limit import reset_rate_limit_buckets
    reset_engine()
    reset_idempotency_store()
    reset_rate_limit_buckets()
    from alembic.config import Config
    from alembic import command
    cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    command.upgrade(cfg, "head")

    from backend.models import Organization, Membership
    from backend.auth.models import User as AuthUser
    db = get_session_factory()()
    org1 = Organization(name="合作社A", created_by="u1")
    db.add(org1)
    db.flush()
    db.add(AuthUser(id="u1", email="u1@test.local", password_hash="x"))
    db.flush()
    db.add(Membership(user_id="u1", org_id=org1.id, role="owner"))
    db.commit()
    oid = org1.id
    db.close()

    from fastapi.testclient import TestClient
    from backend.app import app
    app.dependency_overrides.clear()

    def _override_db():
        s = get_session_factory()()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app, raise_server_exceptions=False)

    class Env:
        org1 = oid

        def as_user(self, uid):
            def _u():
                return AuthUser(id=uid, email="%s@test.local" % uid,
                                password_hash="x")
            app.dependency_overrides[get_current_user] = _u
            return client

    yield Env()
    app.dependency_overrides.clear()
    reset_engine()
    reset_idempotency_store()
    reset_rate_limit_buckets()
