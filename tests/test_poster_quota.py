"""海报 AI 生图限额：普通用户每天 5 次，admin/owner 豁免。

- 迁移 0022 建 poster_image_usage 表
- get_poster_quota：剩余额度计算；豁免角色不限
- _check_poster_quota：超限抛 429（AI_RATE_LIMITED）
- generate_poster_image：成功记一条用量（mock 图片 API，不调真实服务）

业务库 backend/data/lupin.db 只读，绝不写入/迁移：全部用 scratch 库。

运行：./venv/bin/python -m pytest tests/test_poster_quota.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.auth.database import (  # noqa: E402
    get_session_factory,
    reset_engine,
)
from backend.auth.models import User as AuthUser  # noqa: E402
from backend.core import error_codes  # noqa: E402
from backend.core.exceptions import BusinessError  # noqa: E402
from backend.models import Membership, Organization, PosterImageUsage  # noqa: E402
from backend.api.v1.poster_image import (  # noqa: E402
    POSTER_IMAGE_DAILY_LIMIT,
    _check_poster_quota,
    create_image_job,
    generate_poster_image,
    get_image_job,
    get_poster_quota,
    image_quota,
    ImageGenReq,
)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/poster_quota.db"
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


def _setup(db, role="editor", user_id="u1"):
    org = Organization(name="海报限额合作社", created_by="u1")
    db.add(org)
    db.commit()
    db.add(AuthUser(id=user_id, email=f"{user_id}@t.local",
                    password_hash="x"))
    db.add(Membership(org_id=org.id, user_id=user_id, role=role))
    db.commit()
    return org


def _use(db, user_id, org_id, n, days_ago=0):
    base = datetime.now(timezone.utc) - timedelta(days=days_ago)
    for _ in range(n):
        db.add(PosterImageUsage(user_id=user_id, org_id=org_id,
                                created_at=base))
    db.commit()


class TestQuotaCalc:
    def test_regular_user_remaining(self, db):
        org = _setup(db, role="editor")
        _use(db, "u1", org.id, 2)
        q = get_poster_quota(db, "u1", org.id, "editor")
        assert q == {"limited": True, "limit": 5, "used": 2, "remaining": 3}

    def test_yesterday_not_counted(self, db):
        org = _setup(db, role="editor")
        _use(db, "u1", org.id, 5, days_ago=1)
        q = get_poster_quota(db, "u1", org.id, "editor")
        assert q["remaining"] == 5

    def test_admin_exempt(self, db):
        org = _setup(db, role="admin", user_id="u_admin")
        _use(db, "u_admin", org.id, 10)
        q = get_poster_quota(db, "u_admin", org.id, "admin")
        assert q == {"limited": False, "limit": None, "used": None,
                     "remaining": None}

    def test_owner_exempt(self, db):
        org = _setup(db, role="owner", user_id="u_owner")
        q = get_poster_quota(db, "u_owner", org.id, "owner")
        assert q["limited"] is False

    def test_check_raises_429_when_exhausted(self, db):
        org = _setup(db, role="editor")
        _use(db, "u1", org.id, 5)
        with pytest.raises(BusinessError) as ei:
            _check_poster_quota(db, "u1", org.id, "editor")
        assert ei.value.http_status == 429
        assert ei.value.code == error_codes.AI_RATE_LIMITED

    def test_check_passes_when_remaining(self, db):
        org = _setup(db, role="editor")
        _use(db, "u1", org.id, 4)
        _check_poster_quota(db, "u1", org.id, "editor")  # 不抛


class TestGenerateEndpoint:
    def test_success_records_usage(self, db, monkeypatch):
        org = _setup(db, role="editor")
        monkeypatch.setenv("LUPIN_IMAGE_API_KEY", "test-key")
        monkeypatch.setattr(
            "backend.api.v1.poster_image._call_image_api",
            lambda *a, **k: {"b64_json": "abc"})
        user = db.query(AuthUser).filter_by(id="u1").one()
        out = generate_poster_image(
            ImageGenReq(org_id=org.id, prompt="红苹果", size="1024x1024"),
            user, db)
        assert out["data"]["image_b64"] == "abc"
        n = db.query(PosterImageUsage).filter_by(user_id="u1").count()
        assert n == 1

    def test_blocked_at_limit(self, db, monkeypatch):
        org = _setup(db, role="editor")
        monkeypatch.setenv("LUPIN_IMAGE_API_KEY", "test-key")
        _use(db, "u1", org.id, 5)
        called = []
        monkeypatch.setattr(
            "backend.api.v1.poster_image._call_image_api",
            lambda *a, **k: called.append(1) or {"b64_json": "x"})
        user = db.query(AuthUser).filter_by(id="u1").one()
        with pytest.raises(BusinessError) as ei:
            generate_poster_image(
                ImageGenReq(org_id=org.id, prompt="红苹果",
                            size="1024x1024"),
                user, db)
        assert ei.value.http_status == 429
        assert called == [], "超限不应调用图片服务"

    def test_admin_not_limited(self, db, monkeypatch):
        org = _setup(db, role="admin", user_id="u_admin")
        monkeypatch.setenv("LUPIN_IMAGE_API_KEY", "test-key")
        _use(db, "u_admin", org.id, 9)
        monkeypatch.setattr(
            "backend.api.v1.poster_image._call_image_api",
            lambda *a, **k: {"b64_json": "z"})
        user = db.query(AuthUser).filter_by(id="u_admin").one()
        out = generate_poster_image(
            ImageGenReq(org_id=org.id, prompt="红苹果", size="1024x1024"),
            user, db)
        assert out["data"]["image_b64"] == "z"

    def test_quota_endpoint(self, db):
        org = _setup(db, role="editor")
        _use(db, "u1", org.id, 3)
        user = db.query(AuthUser).filter_by(id="u1").one()
        out = image_quota(org.id, user, db)
        assert out["data"]["remaining"] == 2
        assert POSTER_IMAGE_DAILY_LIMIT == 5


# ---------------------------------------------------------------------------
# 异步任务（2026-10-22 加）：绕开网关 60s 硬超时
# ---------------------------------------------------------------------------

class TestImageJobs:
    """POST /poster/image/jobs 立即返回 job_id，后台线程出图，前端轮询。"""

    def _wait(self, job_id, user_id="u1", timeout=5.0):
        import time

        from backend.services import poster_jobs
        end = time.time() + timeout
        while time.time() < end:
            job = poster_jobs.get_job(job_id, user_id=user_id)
            if job is not None and job.status != poster_jobs.STATUS_RUNNING:
                return job
            time.sleep(0.05)
        raise AssertionError(f"任务 {job_id} 5s 内未完成")

    def test_returns_immediately_then_done(self, db, monkeypatch):
        import time

        from backend.services import poster_jobs
        poster_jobs.reset_for_tests()
        org = _setup(db, role="editor")
        monkeypatch.setenv("LUPIN_IMAGE_API_KEY", "test-key")

        def _slow(*a, **k):
            time.sleep(0.3)
            return {"b64_json": "IMG"}

        monkeypatch.setattr(
            "backend.api.v1.poster_image._call_image_api", _slow)
        user = db.query(AuthUser).filter_by(id="u1").one()

        t0 = time.time()
        out = create_image_job(
            ImageGenReq(org_id=org.id, prompt="红苹果", size="1024x1024"),
            user, db)
        submit_secs = time.time() - t0
        assert submit_secs < 0.25, f"提交应毫秒级返回，实测 {submit_secs:.2f}s"
        jid = out["data"]["job_id"]
        assert out["data"]["status"] == poster_jobs.STATUS_RUNNING
        assert out["data"]["job_id"]

        job = self._wait(jid, "u1")
        assert job.status == poster_jobs.STATUS_DONE
        assert job.image_b64 == "IMG"

    def test_success_records_usage_once(self, db, monkeypatch):
        from backend.services import poster_jobs
        poster_jobs.reset_for_tests()
        org = _setup(db, role="editor")
        monkeypatch.setenv("LUPIN_IMAGE_API_KEY", "test-key")
        monkeypatch.setattr(
            "backend.api.v1.poster_image._call_image_api",
            lambda *a, **k: {"b64_json": "IMG"})
        user = db.query(AuthUser).filter_by(id="u1").one()
        out = create_image_job(
            ImageGenReq(org_id=org.id, prompt="红苹果", size="1024x1024"),
            user, db)
        self._wait(out["data"]["job_id"], "u1")
        # 记账由后台线程用独立会话提交，这里用新会话复核
        sess = get_session_factory()()
        try:
            n = sess.query(PosterImageUsage).filter_by(user_id="u1").count()
        finally:
            sess.close()
        assert n == 1

    def test_failure_sets_failed_without_usage(self, db, monkeypatch):
        from backend.services import poster_jobs
        poster_jobs.reset_for_tests()
        org = _setup(db, role="editor")
        monkeypatch.setenv("LUPIN_IMAGE_API_KEY", "test-key")

        def _boom(*a, **k):
            raise BusinessError("图片服务主备通道均失败：x",
                                code=error_codes.AI_PROVIDER_ERROR,
                                http_status=502)

        monkeypatch.setattr(
            "backend.api.v1.poster_image._call_image_api", _boom)
        user = db.query(AuthUser).filter_by(id="u1").one()
        out = create_image_job(
            ImageGenReq(org_id=org.id, prompt="红苹果", size="1024x1024"),
            user, db)
        job = self._wait(out["data"]["job_id"], "u1")
        assert job.status == poster_jobs.STATUS_FAILED
        assert job.image_b64 is None
        assert job.error_code == error_codes.AI_PROVIDER_ERROR
        sess = get_session_factory()()
        try:
            n = sess.query(PosterImageUsage).filter_by(user_id="u1").count()
        finally:
            sess.close()
        assert n == 0, "失败不应记用量"

    def test_get_job_hides_other_users_job(self, db, monkeypatch):
        from backend.services import poster_jobs
        poster_jobs.reset_for_tests()
        org = _setup(db, role="editor")
        other = AuthUser(id="u2", email="u2@t.local", password_hash="x")
        db.add(other)
        db.add(Membership(org_id=org.id, user_id="u2", role="editor"))
        db.commit()
        monkeypatch.setenv("LUPIN_IMAGE_API_KEY", "test-key")
        monkeypatch.setattr(
            "backend.api.v1.poster_image._call_image_api",
            lambda *a, **k: {"b64_json": "IMG"})
        user = db.query(AuthUser).filter_by(id="u1").one()
        out = create_image_job(
            ImageGenReq(org_id=org.id, prompt="红苹果", size="1024x1024"),
            user, db)
        jid = out["data"]["job_id"]
        assert get_image_job(jid, user)["data"]["job_id"] == jid
        u2 = db.query(AuthUser).filter_by(id="u2").one()
        with pytest.raises(BusinessError) as ei:
            get_image_job(jid, u2)
        assert ei.value.http_status == 404

    def test_unknown_job_404(self, db):
        _setup(db, role="editor")
        user = db.query(AuthUser).filter_by(id="u1").one()
        with pytest.raises(BusinessError) as ei:
            get_image_job("no-such-job", user)
        assert ei.value.http_status == 404

    def test_watchdog_marks_stale_running_as_failed(self, db):
        """上游挂住时，超过 JOB_TIMEOUT_SECONDS 仍 running 的任务判失败。"""
        import time as _t

        from backend.services import poster_jobs
        poster_jobs.reset_for_tests()
        job = poster_jobs.create_job("u1", "org-x")
        job.created_at = _t.time() - (poster_jobs.JOB_TIMEOUT_SECONDS + 5)
        got = poster_jobs.get_job(job.id, user_id="u1")
        assert got.status == poster_jobs.STATUS_FAILED
        assert got.error_code == error_codes.AI_PROVIDER_ERROR
        assert "超时" in got.error

    def test_watchdog_leaves_fresh_job_running(self, db):
        from backend.services import poster_jobs
        poster_jobs.reset_for_tests()
        job = poster_jobs.create_job("u1", "org-x")
        got = poster_jobs.get_job(job.id, user_id="u1")
        assert got.status == poster_jobs.STATUS_RUNNING
        assert got.public().get("elapsed") is not None

    def test_job_blocked_at_limit(self, db, monkeypatch):
        from backend.services import poster_jobs
        poster_jobs.reset_for_tests()
        org = _setup(db, role="editor")
        _use(db, "u1", org.id, 5)
        monkeypatch.setenv("LUPIN_IMAGE_API_KEY", "test-key")
        called = []
        monkeypatch.setattr(
            "backend.api.v1.poster_image._call_image_api",
            lambda *a, **k: called.append(1) or {"b64_json": "x"})
        user = db.query(AuthUser).filter_by(id="u1").one()
        with pytest.raises(BusinessError) as ei:
            create_image_job(
                ImageGenReq(org_id=org.id, prompt="红苹果",
                            size="1024x1024"),
                user, db)
        assert ei.value.http_status == 429
        assert called == [], "超限不应提交任务"
