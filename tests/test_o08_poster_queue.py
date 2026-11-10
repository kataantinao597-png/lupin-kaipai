"""O-08：海报排队位置与预计等待测试（CASE-20261109-005）。

- public() 带 position（前面有几个未完成任务，全局 FIFO）与
  eta_seconds 区间 {"min": .., "max": ..}；
- 终态任务 position=0、eta 为 0；
- 已开始执行的任务按已耗时折算、上限受看门狗 200s 约束；
- POST /image/jobs 创建响应同样带 position / eta_seconds。
"""
import os
import sys
import time

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import backend.auth  # noqa: F401,E402  先 import，保证包初始化顺序


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """带认证覆盖的 TestClient：user=owner（复用 test_poster_image 形态）。"""
    url = f"sqlite:///{tmp_path}/o08.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    monkeypatch.setenv("LUPIN_MEDIA_DIR", str(tmp_path / "media_files"))
    monkeypatch.setenv("LUPIN_MEDIA_SIGN_SECRET", "test-sign-secret")
    monkeypatch.delenv("LUPIN_IMAGE_API_KEY", raising=False)
    monkeypatch.delenv("LUPIN_IMAGE_API_BASE", raising=False)
    from backend.auth.database import reset_engine, get_session_factory, get_db
    from backend.auth.deps import get_current_user
    from backend.media import reset_storage
    from backend.media.signing import reset_sign_secret
    from backend.services import poster_jobs
    reset_engine()
    reset_storage()
    reset_sign_secret()
    poster_jobs.reset_for_tests()
    from alembic.config import Config
    from alembic import command
    cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    command.upgrade(cfg, "heads")

    from backend.models import Organization, Membership, Work
    from backend.auth.models import User as AuthUser
    db = get_session_factory()()
    org = Organization(name="生图测试社", created_by="u_owner")
    db.add(org)
    db.flush()
    user = AuthUser(id="u_owner", email="owner@test.local", password_hash="x")
    db.add(user)
    db.flush()
    db.add(Membership(user_id="u_owner", org_id=org.id, role="owner"))
    work = Work(org_id=org.id, owner_id="u_owner", title="测试作品",
                data_json='{"product": {}}')
    db.add(work)
    db.commit()
    org_id, work_id = org.id, work.id
    db.close()

    from fastapi.testclient import TestClient
    from backend.app import app

    def _override_user():
        return AuthUser(id="u_owner", email="owner@test.local",
                        password_hash="x")

    def _override_db():
        s = get_session_factory()()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app, raise_server_exceptions=False)
    client.headers["Authorization"] = "Bearer o08-test-isolation"
    yield client, org_id, work_id
    app.dependency_overrides.clear()
    poster_jobs.reset_for_tests()
    reset_engine()
    reset_storage()


class TestPosterQueueInfo:
    def test_positions_and_eta_range(self):
        from backend.services import poster_jobs
        poster_jobs.reset_for_tests()
        try:
            j1 = poster_jobs.create_job("u1", "o1")
            j2 = poster_jobs.create_job("u1", "o1")
            j3 = poster_jobs.create_job("u1", "o1")
            p1 = j1.public()
            p3 = j3.public()
            assert p1["position"] == 0
            assert p1["eta_seconds"] == {"min": 30, "max": 90}
            assert p3["position"] == 2
            # 第 3 个任务：自己 30–90s + 前面 2 个各 30–90s
            assert p3["eta_seconds"] == {"min": 90, "max": 270}
        finally:
            poster_jobs.reset_for_tests()

    def test_terminal_job_has_zero_position_and_eta(self):
        from backend.services import poster_jobs
        poster_jobs.reset_for_tests()
        try:
            j = poster_jobs.create_job("u1", "o1")
            poster_jobs._finish(j, status=poster_jobs.STATUS_DONE,
                                image_b64="xx")
            p = j.public()
            assert p["status"] == "done"
            assert p["position"] == 0
            assert p["eta_seconds"] == {"min": 0, "max": 0}
        finally:
            poster_jobs.reset_for_tests()

    def test_started_job_eta_discounts_elapsed(self):
        from backend.services import poster_jobs
        poster_jobs.reset_for_tests()
        try:
            j = poster_jobs.create_job("u1", "o1")
            # 已执行 100s：下限 max(0, 30-100)=0，上限受看门狗约束 200-100=100
            # （计算时刻有毫秒级流逝，int 截断后允许 99~100）
            j.started_at = time.time() - 100
            p = j.public()
            assert p["position"] == 0
            assert p["eta_seconds"]["min"] == 0
            assert p["eta_seconds"]["max"] in (99, 100)
        finally:
            poster_jobs.reset_for_tests()

    def test_create_job_response_carries_queue_info(
            self, api_client, monkeypatch):
        from backend.api.v1 import poster_image as pi
        # 上游假成功（0.5s 后返回）：创建响应发出时任务仍在 running，
        # 可断言 running 态的排队信息；随后轮询等它落 done。
        def _fake_call(prompt, size, total_timeout=None):
            time.sleep(0.5)
            return {"b64_json": "aGVsbG8="}

        monkeypatch.setattr(pi, "_call_image_api", _fake_call)
        client, org_id, _ = api_client
        r = client.post("/api/v1/poster/image/jobs",
                        json={"org_id": org_id, "prompt": "一只猫",
                              "size": "1024x1024"})
        assert r.status_code == 202, r.text
        d = r.json()["data"]
        assert d["job_id"]
        assert d["status"] == "running"
        assert d["position"] == 0
        assert d["eta_seconds"] == {"min": 30, "max": 90}
        # 轮询：完成后 position=0、eta 为 0（等后台线程落 done，最多 5s）
        job_id = d["job_id"]
        t0 = time.time()
        got = None
        while time.time() - t0 < 5:
            rr = client.get(f"/api/v1/poster/image/jobs/{job_id}")
            assert rr.status_code == 200, rr.text
            got = rr.json()["data"]
            if got["status"] == "done":
                break
            time.sleep(0.1)
        assert got["status"] == "done"
        assert got["position"] == 0
        assert got["eta_seconds"] == {"min": 0, "max": 0}
