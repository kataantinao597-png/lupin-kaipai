"""V3.0 F：发布中心测试。

- 发布记录 CRUD、状态机转换表（非法跳过被拒）
- 发布前检查：缺项被拦、重大风险未处理阻止审核通过
- 各阶段独立：文案确认/剪辑完成不自动推进发布状态
- 已发布记录关联锁定、已发布不可删
- 导出发布包含文案与检查清单、无"自动发布"字样
- 权限隔离（跨组织 404、防枚举）
"""
import os
import sys

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import backend.auth  # noqa: F401,E402  先 import，保证包初始化顺序


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/publish.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    monkeypatch.delenv("LUPIN_API_KEY", raising=False)
    from backend.auth.database import (
        reset_engine, get_session_factory, get_db,
    )
    from backend.auth.deps import get_current_user
    from backend.security.rate_limit import reset_rate_limit_buckets
    from backend.core.idempotency import reset_idempotency_store
    reset_engine()
    reset_rate_limit_buckets()
    reset_idempotency_store()
    from alembic.config import Config
    from alembic import command
    cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    command.upgrade(cfg, "head")

    from backend.models import Organization, Membership
    from backend.auth.models import User as AuthUser
    db = get_session_factory()()
    org1 = Organization(name="合作社A", created_by="u1")
    org2 = Organization(name="合作社B", created_by="u2")
    db.add(org1)
    db.add(org2)
    db.flush()
    for uid in ("u1", "u2", "u_viewer"):
        db.add(AuthUser(id=uid, email=f"{uid}@test.local", password_hash="x"))
    db.flush()
    db.add(Membership(user_id="u1", org_id=org1.id, role="owner"))
    db.add(Membership(user_id="u2", org_id=org2.id, role="owner"))
    db.add(Membership(user_id="u_viewer", org_id=org1.id, role="viewer"))
    db.commit()
    ids = (org1.id, org2.id)
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
        org1, org2 = ids

        def as_user(self, uid):
            def _u():
                return AuthUser(id=uid, email=f"{uid}@test.local",
                                password_hash="x")
            app.dependency_overrides[get_current_user] = _u
            return client

        def db(self):
            return get_session_factory()()

    yield Env()
    app.dependency_overrides.clear()
    reset_engine()
    reset_rate_limit_buckets()
    reset_idempotency_store()


def _doc_with_confirmed(client, org_id):
    """建文档（带原文 → v1 即确认版本），返回 (doc_id, version_id)。"""
    r = client.post("/api/v1/copy/docs", params={"org_id": org_id},
                    json={"title": "测试文案", "kind": "optimize",
                          "original_text": "原文：这樱桃真甜，果大汁多。"})
    assert r.status_code == 201, r.text
    doc_id = r.json()["data"]["id"]
    r = client.get(f"/api/v1/copy/docs/{doc_id}", params={"org_id": org_id})
    assert r.status_code == 200, r.text
    cur = r.json()["data"]["current_version"]
    assert cur["status"] == "confirmed"
    return doc_id, cur["id"]


def _platform_version(client, org_id, doc_id, version_id):
    """手动建完整平台版本（标题/正文/话题/封面齐全）。"""
    r = client.post(f"/api/v1/platforms/copy/docs/{doc_id}/platforms",
                    params={"org_id": org_id},
                    json={"copy_version_id": version_id, "platform": "douyin",
                          "use_ai": False, "title": "抖音标题",
                          "body": "抖音正文，果大汁多。",
                          "hashtags": ["#烟台大樱桃"],
                          "cover_text": "果大汁多", "cta": "点击购买"})
    assert r.status_code == 201, r.text
    return r.json()["data"]["platform_version"]["id"]


def _seed_db(env, org_id):
    """直插 work + ready asset，返回 (work_id, asset_id)。"""
    from backend import models
    db = env.db()
    work = models.Work(org_id=org_id, owner_id="u1", title="樱桃拍摄项目")
    db.add(work)
    db.flush()
    asset = models.Asset(org_id=org_id, work_id=work.id,
                         key=f"{org_id}/{work.id}/final.mp4",
                         filename="final.mp4", mime="video/mp4",
                         size=1024, status=models.ASSET_STATUS_READY,
                         created_by="u1")
    db.add(asset)
    db.commit()
    ids = (work.id, asset.id)
    db.close()
    return ids


def _make_record(client, org_id, **kw):
    body = {"title": "樱桃抖音发布", "platform": "douyin"}
    body.update(kw)
    return client.post("/api/v1/publish/records",
                       params={"org_id": org_id}, json=body)


def _set_open_risk(env, org_id, version_id):
    """给文案版本塞一条未处理的重大风险（open + 高）。"""
    from backend import models
    from sqlalchemy import select
    db = env.db()
    v = db.execute(select(models.CopyVersion).where(
        models.CopyVersion.id == version_id)).scalar_one()
    v.risk_flags = [{"text": "宣称'全国第一甜'无依据", "level": "高",
                     "reason": "缺少依据", "status": "open",
                     "shot_number": 1}]
    v.risk_status = models.COPY_RISK_CHECKED
    db.commit()
    db.close()


class TestPublishCRUD:
    def test_create_pending(self, api_env):
        client = api_env.as_user("u1")
        r = _make_record(client, api_env.org1)
        assert r.status_code == 201, r.text
        d = r.json()["data"]
        assert d["status"] == "pending"
        assert d["platform"] == "douyin"

    def test_create_unsupported_platform_400(self, api_env):
        client = api_env.as_user("u1")
        r = _make_record(client, api_env.org1, platform="nope")
        assert r.status_code == 400, r.text

    def test_list_filter_status(self, api_env):
        client = api_env.as_user("u1")
        _make_record(client, api_env.org1, title="A")
        r = client.get("/api/v1/publish/records",
                       params={"org_id": api_env.org1, "status": "pending"})
        assert r.status_code == 200, r.text
        assert len(r.json()["data"]) >= 1

    def test_cross_org_404(self, api_env):
        c1 = api_env.as_user("u1")
        r = _make_record(c1, api_env.org1)
        rec_id = r.json()["data"]["id"]
        c2 = api_env.as_user("u2")
        r = c2.get(f"/api/v1/publish/records/{rec_id}",
                   params={"org_id": api_env.org2})
        assert r.status_code == 404, r.text

    def test_viewer_read_only(self, api_env):
        c1 = api_env.as_user("u1")
        _make_record(c1, api_env.org1)
        cv = api_env.as_user("u_viewer")
        r = cv.get("/api/v1/publish/records",
                   params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        r = _make_record(cv, api_env.org1)
        assert r.status_code == 404, r.text  # 防枚举：无权按不存在处理


class TestTransitions:
    def test_pending_to_review(self, api_env):
        client = api_env.as_user("u1")
        rec_id = _make_record(client, api_env.org1).json()["data"]["id"]
        r = client.post(f"/api/v1/publish/records/{rec_id}/transition",
                        params={"org_id": api_env.org1},
                        json={"to_status": "review"})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["status"] == "review"

    def test_skip_transition_rejected(self, api_env):
        """pending 不能直接到 published（各阶段独立）。"""
        client = api_env.as_user("u1")
        rec_id = _make_record(client, api_env.org1).json()["data"]["id"]
        r = client.post(f"/api/v1/publish/records/{rec_id}/transition",
                        params={"org_id": api_env.org1},
                        json={"to_status": "published"})
        assert r.status_code == 400, r.text
        assert r.json()["code"] == 3015

    def test_review_to_ready_blocked_by_check(self, api_env):
        """检查缺项（无视频/文案未确认/未授权）→ 阻止，400/3016。"""
        client = api_env.as_user("u1")
        rec_id = _make_record(client, api_env.org1).json()["data"]["id"]
        client.post(f"/api/v1/publish/records/{rec_id}/transition",
                    params={"org_id": api_env.org1},
                    json={"to_status": "review"})
        r = client.post(f"/api/v1/publish/records/{rec_id}/transition",
                        params={"org_id": api_env.org1},
                        json={"to_status": "ready"})
        assert r.status_code == 400, r.text
        assert r.json()["code"] == 3016
        # 检查结果已落盘
        r = client.get(f"/api/v1/publish/records/{rec_id}",
                       params={"org_id": api_env.org1})
        items = r.json()["data"]["check_result"]
        assert len(items) == 6
        by_key = {i["key"]: i for i in items}
        assert by_key["video_ready"]["passed"] is False
        assert by_key["video_ready"]["blocking"] is True
        assert by_key["copy_confirmed"]["passed"] is False

    def test_review_to_ready_after_fix(self, api_env):
        """补齐视频/确认文案/完整平台版本/授权 → 检查通过 → 可流转。"""
        client = api_env.as_user("u1")
        work_id, asset_id = _seed_db(api_env, api_env.org1)
        doc_id, ver_id = _doc_with_confirmed(client, api_env.org1)
        pv_id = _platform_version(client, api_env.org1, doc_id, ver_id)
        rec_id = _make_record(
            client, api_env.org1, work_id=work_id,
            copy_version_id=ver_id, platform_version_id=pv_id,
            video_asset_id=asset_id).json()["data"]["id"]
        # 人工确认授权
        r = client.patch(f"/api/v1/publish/records/{rec_id}",
                         params={"org_id": api_env.org1},
                         json={"authorization_confirmed": True})
        assert r.status_code == 200, r.text
        r = client.post(f"/api/v1/publish/records/{rec_id}/transition",
                        params={"org_id": api_env.org1},
                        json={"to_status": "review"})
        assert r.status_code == 200, r.text
        r = client.post(f"/api/v1/publish/records/{rec_id}/check",
                        params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["blocking_passed"] is True
        r = client.post(f"/api/v1/publish/records/{rec_id}/transition",
                        params={"org_id": api_env.org1},
                        json={"to_status": "ready"})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["status"] == "ready"

    def test_risk_open_blocks_review(self, api_env):
        """未处理的重大风险 → 阻止显示为审核通过。"""
        client = api_env.as_user("u1")
        work_id, asset_id = _seed_db(api_env, api_env.org1)
        doc_id, ver_id = _doc_with_confirmed(client, api_env.org1)
        _set_open_risk(api_env, api_env.org1, ver_id)
        rec_id = _make_record(
            client, api_env.org1, work_id=work_id,
            copy_version_id=ver_id, video_asset_id=asset_id).json()["data"]["id"]
        client.patch(f"/api/v1/publish/records/{rec_id}",
                     params={"org_id": api_env.org1},
                     json={"authorization_confirmed": True})
        client.post(f"/api/v1/publish/records/{rec_id}/transition",
                    params={"org_id": api_env.org1},
                    json={"to_status": "review"})
        r = client.post(f"/api/v1/publish/records/{rec_id}/transition",
                        params={"org_id": api_env.org1},
                        json={"to_status": "ready"})
        assert r.status_code == 400, r.text
        assert r.json()["code"] == 3016
        assert "重大风险" in r.json()["message"]

    def test_ready_to_published_needs_url(self, api_env):
        client = api_env.as_user("u1")
        work_id, asset_id = _seed_db(api_env, api_env.org1)
        doc_id, ver_id = _doc_with_confirmed(client, api_env.org1)
        rec_id = _make_record(
            client, api_env.org1, work_id=work_id,
            copy_version_id=ver_id, video_asset_id=asset_id).json()["data"]["id"]
        client.patch(f"/api/v1/publish/records/{rec_id}",
                     params={"org_id": api_env.org1},
                     json={"authorization_confirmed": True})
        client.post(f"/api/v1/publish/records/{rec_id}/transition",
                    params={"org_id": api_env.org1},
                    json={"to_status": "review"})
        client.post(f"/api/v1/publish/records/{rec_id}/transition",
                    params={"org_id": api_env.org1},
                    json={"to_status": "ready"})
        # 缺发布时间和链接 → 400
        r = client.post(f"/api/v1/publish/records/{rec_id}/transition",
                        params={"org_id": api_env.org1},
                        json={"to_status": "published"})
        assert r.status_code == 400, r.text
        # 补上 → 通过
        r = client.post(f"/api/v1/publish/records/{rec_id}/transition",
                        params={"org_id": api_env.org1},
                        json={"to_status": "published",
                              "published_at": "2026-10-19T18:00:00+08:00",
                              "publish_url": "https://v.douyin.com/abc123/"})
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["status"] == "published"
        assert d["publish_url"] == "https://v.douyin.com/abc123/"

    def test_published_to_reviewed(self, api_env):
        client = api_env.as_user("u1")
        rec_id = _make_record(client, api_env.org1).json()["data"]["id"]
        for to_s, extra in [("review", {}), ("ready", {}),
                            ("published", {"published_at":
                                           "2026-10-19T18:00:00+08:00",
                                           "publish_url": "https://x/y"})]:
            if to_s == "ready":
                # 先补齐检查项：直插最小可用关联
                work_id, asset_id = _seed_db(api_env, api_env.org1)
                doc_id, ver_id = _doc_with_confirmed(client, api_env.org1)
                client.patch(f"/api/v1/publish/records/{rec_id}",
                             params={"org_id": api_env.org1},
                             json={"work_id": work_id,
                                   "copy_version_id": ver_id,
                                   "video_asset_id": asset_id,
                                   "authorization_confirmed": True})
            body = {"to_status": to_s}
            body.update(extra)
            r = client.post(f"/api/v1/publish/records/{rec_id}/transition",
                            params={"org_id": api_env.org1}, json=body)
            assert r.status_code == 200, (to_s, r.text)
        r = client.post(f"/api/v1/publish/records/{rec_id}/transition",
                        params={"org_id": api_env.org1},
                        json={"to_status": "reviewed"})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["status"] == "reviewed"


class TestPublishLocks:
    def _to_published(self, client, env):
        work_id, asset_id = _seed_db(env, env.org1)
        doc_id, ver_id = _doc_with_confirmed(client, env.org1)
        rec_id = _make_record(
            client, env.org1, work_id=work_id,
            copy_version_id=ver_id, video_asset_id=asset_id).json()["data"]["id"]
        client.patch(f"/api/v1/publish/records/{rec_id}",
                     params={"org_id": env.org1},
                     json={"authorization_confirmed": True})
        client.post(f"/api/v1/publish/records/{rec_id}/transition",
                    params={"org_id": env.org1}, json={"to_status": "review"})
        client.post(f"/api/v1/publish/records/{rec_id}/transition",
                    params={"org_id": env.org1}, json={"to_status": "ready"})
        client.post(f"/api/v1/publish/records/{rec_id}/transition",
                    params={"org_id": env.org1},
                    json={"to_status": "published",
                          "published_at": "2026-10-19T18:00:00+08:00",
                          "publish_url": "https://v.douyin.com/abc123/"})
        return rec_id

    def test_published_locks_associations(self, api_env):
        """已发布后不允许改关联内容，但允许改备注。"""
        client = api_env.as_user("u1")
        rec_id = self._to_published(client, api_env)
        r = client.patch(f"/api/v1/publish/records/{rec_id}",
                         params={"org_id": api_env.org1},
                         json={"work_id": "other"})
        assert r.status_code == 400, r.text
        r = client.patch(f"/api/v1/publish/records/{rec_id}",
                         params={"org_id": api_env.org1},
                         json={"notes": "发布顺利，播放量待录入"})
        assert r.status_code == 200, r.text

    def test_delete_published_blocked(self, api_env):
        client = api_env.as_user("u1")
        rec_id = self._to_published(client, api_env)
        r = client.delete(f"/api/v1/publish/records/{rec_id}",
                          params={"org_id": api_env.org1})
        assert r.status_code == 400, r.text

    def test_delete_pending_ok(self, api_env):
        client = api_env.as_user("u1")
        rec_id = _make_record(client, api_env.org1).json()["data"]["id"]
        r = client.delete(f"/api/v1/publish/records/{rec_id}",
                          params={"org_id": api_env.org1})
        assert r.status_code == 204, r.text


class TestExport:
    def test_export_txt(self, api_env):
        client = api_env.as_user("u1")
        doc_id, ver_id = _doc_with_confirmed(client, api_env.org1)
        pv_id = _platform_version(client, api_env.org1, doc_id, ver_id)
        rec_id = _make_record(
            client, api_env.org1,
            copy_version_id=ver_id, platform_version_id=pv_id).json()["data"]["id"]
        r = client.get(f"/api/v1/publish/records/{rec_id}/export",
                       params={"org_id": api_env.org1})
        assert r.status_code == 200, r.text
        txt = r.text
        assert "发布包" in txt
        assert "抖音标题" in txt
        assert "烟台大樱桃" in txt  # 平台 API 会剥离 # 前缀
        assert "人工复制发布" in txt
        assert "自动发布" not in txt
