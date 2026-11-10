"""O-04：超时扫描后台线程测试（CASE-20261109-005）。

- 后台线程按周期跑 sweep_timed_out，超时任务无需轮询也会翻转；
- 单轮 sweep 抛错不杀线程（异常自保），错误记进 sweeper_status；
- start 幂等、stop 后线程停止。
"""
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import backend.auth  # noqa: F401,E402  先 import，保证包初始化顺序


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    """独立 fixture：单组织单用户 + scratch 库。"""
    url = "sqlite:///%s/o04.db" % tmp_path
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    monkeypatch.delenv("LUPIN_API_KEY", raising=False)
    from backend.auth.database import reset_engine, get_session_factory
    reset_engine()
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
    yield oid
    from backend.ai_tasks.sweeper import stop_timeout_sweeper
    stop_timeout_sweeper()
    reset_engine()


def _seed_timed_out_pending(org_id, age_seconds=700):
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
        task.status = "pending"
        task.created_at = (datetime.now(timezone.utc)
                           - timedelta(seconds=age_seconds))
        s.commit()
        return tid
    finally:
        s.close()


def _status_of(tid):
    from backend.auth.database import get_session_factory
    from backend.models import AITask
    s = get_session_factory()()
    try:
        return s.get(AITask, tid).status
    finally:
        s.close()


class TestTimeoutSweeper:
    def test_thread_flips_timed_out_without_polling(self, api_env):
        from backend.ai_tasks.sweeper import (
            start_timeout_sweeper, stop_timeout_sweeper, sweeper_status)
        tid = _seed_timed_out_pending(api_env)
        assert _status_of(tid) == "pending"
        start_timeout_sweeper(interval=0.2)
        try:
            t0 = time.time()
            while time.time() - t0 < 5:
                if _status_of(tid) != "pending":
                    break
                time.sleep(0.1)
            assert _status_of(tid) == "cancelled"
            st = sweeper_status()
            assert st["alive"] is True
            assert st["last_swept"] is not None and st["last_swept"] >= 1
            assert st["consecutive_failures"] == 0
        finally:
            stop_timeout_sweeper()

    def test_start_is_idempotent(self, api_env):
        from backend.ai_tasks.sweeper import (
            start_timeout_sweeper, stop_timeout_sweeper)
        start_timeout_sweeper(interval=0.2)
        start_timeout_sweeper(interval=0.2)
        try:
            names = [t.name for t in threading.enumerate()
                     if t.name == "lupin-timeout-sweeper"]
            assert len(names) == 1
        finally:
            stop_timeout_sweeper()
        time.sleep(0.3)
        assert not [t for t in threading.enumerate()
                    if t.name == "lupin-timeout-sweeper"]

    def test_single_sweep_failure_does_not_kill_thread(
            self, api_env, monkeypatch):
        import backend.ai_tasks.sweeper as sweeper_mod
        from backend.ai_tasks.sweeper import (
            start_timeout_sweeper, stop_timeout_sweeper, sweeper_status)
        tid = _seed_timed_out_pending(api_env)
        calls = {"n": 0}
        real_sweep = sweeper_mod.sweep_timed_out

        def flaky(factory):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("模拟 sweep 失败")
            return real_sweep(factory)

        monkeypatch.setattr(sweeper_mod, "sweep_timed_out", flaky)
        start_timeout_sweeper(interval=0.2)
        try:
            t0 = time.time()
            while time.time() - t0 < 5:
                if _status_of(tid) != "pending":
                    break
                time.sleep(0.1)
            # 第一轮失败被记下、线程没死，第二轮成功翻转任务
            assert _status_of(tid) == "cancelled"
            st = sweeper_status()
            assert st["alive"] is True
            assert st["consecutive_failures"] == 0
            assert calls["n"] >= 2
        finally:
            stop_timeout_sweeper()
