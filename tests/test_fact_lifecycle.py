"""S6.1：事实生命周期测试。

- 状态机：合法/非法流转、历史 pending 兼容、reason 强制、确认硬性条件
- Repository：transition 审计、内容版本快照、过期扫库、乐观锁
- Guard：gateway 调用前引用检查（过期/撤销/未确认/不存在一律拒绝）
- API：transition / history 端到端
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 先 import backend.auth，保证包初始化顺序（避开循环导入）
import backend.auth  # noqa: F401,E402

from backend.facts import state_machine as sm  # noqa: E402
from backend.facts.guard import (  # noqa: E402
    UnusableFactError, assert_facts_usable, check_facts_usable,
    extract_fact_ids,
)


# ================= 状态机 =================

class TestStateMachine:
    def test_legal_flow(self):
        assert sm.validate_transition("draft", "pending_review") == \
            ("draft", "pending_review")
        assert sm.validate_transition("pending_review", "confirmed") == \
            ("pending_review", "confirmed")
        assert sm.validate_transition("pending_review", "rejected") == \
            ("pending_review", "rejected")
        assert sm.validate_transition("confirmed", "expired") == \
            ("confirmed", "expired")
        assert sm.validate_transition("confirmed", "revoked") == \
            ("confirmed", "revoked")
        assert sm.validate_transition("expired", "pending_review") == \
            ("expired", "pending_review")
        assert sm.validate_transition("revoked", "pending_review") == \
            ("revoked", "pending_review")
        assert sm.validate_transition("rejected", "draft") == \
            ("rejected", "draft")
        assert sm.validate_transition("rejected", "pending_review") == \
            ("rejected", "pending_review")

    def test_illegal_transitions(self):
        # 草稿不能直接确认；已确认不能退回草稿；过期不能直接确认
        for frm, to in [("draft", "confirmed"), ("draft", "rejected"),
                        ("confirmed", "draft"),
                        ("confirmed", "pending_review"),
                        ("expired", "confirmed"),
                        ("revoked", "confirmed"),
                        ("draft", "expired")]:
            with pytest.raises(sm.IllegalFactTransition):
                sm.validate_transition(frm, to)

    def test_legacy_pending_alias(self):
        # 085 时代的 "pending" 视为 pending_review
        assert sm.normalize_status("pending") == "pending_review"
        assert sm.validate_transition("pending", "confirmed") == \
            ("pending_review", "confirmed")

    def test_unknown_status(self):
        with pytest.raises(sm.FactTransitionRuleError):
            sm.validate_transition("draft", "nope")
        with pytest.raises(sm.FactTransitionRuleError):
            sm.validate_transition("nope", "draft")

    def test_reason_required(self):
        assert sm.requires_reason("confirmed") is True
        assert sm.requires_reason("rejected") is True
        assert sm.requires_reason("revoked") is True
        assert sm.requires_reason("pending_review") is False
        assert sm.requires_reason("expired") is False

    def test_confirm_requirements(self):
        sm.check_confirm_requirements(True, "u1")  # 合法不抛错
        with pytest.raises(sm.FactTransitionRuleError):
            sm.check_confirm_requirements(False, "u1")
        with pytest.raises(sm.FactTransitionRuleError):
            sm.check_confirm_requirements(None, "u1")
        with pytest.raises(sm.FactTransitionRuleError):
            sm.check_confirm_requirements(True, "")
        with pytest.raises(sm.FactTransitionRuleError):
            sm.check_confirm_requirements(True, "  ")


# ================= Repository =================

@pytest.fixture()
def db(tmp_path, monkeypatch):
    """每个用例一个全新的 SQLite 文件库（alembic 建表到最新 head）。"""
    url = f"sqlite:///{tmp_path}/t.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    from backend.auth.database import reset_engine, get_session_factory
    reset_engine()
    from alembic.config import Config
    from alembic import command
    cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    command.upgrade(cfg, "head")
    session = get_session_factory()()
    yield session
    session.close()
    reset_engine()


def _org(db, name="测试合作社"):
    from backend.models import Organization
    org = Organization(name=name, created_by="u1")
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _make_fact(db, org_id, **kw):
    from backend.repositories.fact_repository import FactRepository
    repo = FactRepository(db)
    kw.setdefault("content", "山东大花生，颗粒饱满")
    return repo.create(org_id=org_id, **kw), repo


class TestTransition:
    def test_create_starts_at_draft(self, db):
        org = _org(db)
        fact, _ = _make_fact(db, org.id)
        assert fact.status == "draft"

    def test_full_lifecycle_with_audit(self, db):
        org = _org(db)
        fact, repo = _make_fact(db, org.id)
        # draft -> pending_review（提交，无需 reason）
        fact = repo.transition(fact.id, org.id, "pending_review",
                               operator_id="editor1")
        assert fact.status == "pending_review"
        # pending_review -> confirmed（需 reason + allow_use）
        fact = repo.transition(fact.id, org.id, "confirmed",
                               operator_id="reviewer1",
                               reason="已核对检测报告",
                               evidence_ref="report-2026-001.pdf",
                               expected_version=fact.version)
        assert fact.status == "confirmed"
        assert fact.allow_use is True
        assert fact.verified_by == "reviewer1"
        assert fact.verified_at is not None
        # 审计记录完整
        hist = repo.list_history(fact.id, org.id)
        assert len(hist) == 2
        assert hist[0].to_status == "confirmed"
        assert hist[0].operator_id == "reviewer1"
        assert hist[0].reason == "已核对检测报告"
        assert hist[0].evidence_ref == "report-2026-001.pdf"
        assert hist[1].to_status == "pending_review"
        # confirmed -> revoked（需 reason）
        fact = repo.transition(fact.id, org.id, "revoked",
                               operator_id="reviewer1",
                               reason="报告被撤回",
                               expected_version=fact.version)
        assert fact.status == "revoked"
        assert len(repo.list_history(fact.id, org.id)) == 3

    def test_illegal_transition_rejected(self, db):
        from backend.repositories.fact_repository import FactTransitionError
        org = _org(db)
        fact, repo = _make_fact(db, org.id)
        with pytest.raises(FactTransitionError):
            repo.transition(fact.id, org.id, "confirmed",
                            operator_id="u1", reason="想直接确认")
        # 状态未变，也无审计记录
        assert repo.get(fact.id, org.id).status == "draft"
        assert repo.list_history(fact.id, org.id) == []

    def test_reason_required_for_confirm_reject_revoke(self, db):
        from backend.repositories.fact_repository import FactTransitionError
        org = _org(db)
        fact, repo = _make_fact(db, org.id)
        fact = repo.transition(fact.id, org.id, "pending_review",
                               operator_id="u1")
        # 确认缺 reason 拒绝
        with pytest.raises(FactTransitionError):
            repo.transition(fact.id, org.id, "confirmed", operator_id="u1")
        # 驳回缺 reason 拒绝
        with pytest.raises(FactTransitionError):
            repo.transition(fact.id, org.id, "rejected", operator_id="u1")
        assert repo.get(fact.id, org.id).status == "pending_review"

    def test_confirm_requires_allow_use(self, db):
        from backend.repositories.fact_repository import FactTransitionError
        org = _org(db)
        fact, repo = _make_fact(db, org.id)
        fact = repo.transition(fact.id, org.id, "pending_review",
                               operator_id="u1")
        # 即使 reason 齐全，allow_use=false 也不能确认
        with pytest.raises(FactTransitionError):
            repo.transition(fact.id, org.id, "confirmed", operator_id="u1",
                            reason="ok", allow_use=False)

    def test_version_conflict(self, db):
        from backend.repositories.base import OptimisticLockError
        org = _org(db)
        fact, repo = _make_fact(db, org.id)
        with pytest.raises(OptimisticLockError):
            repo.transition(fact.id, org.id, "pending_review",
                            operator_id="u1", expected_version=999)

    def test_fact_changed_event_published(self, db):
        org = _org(db)
        fact, repo = _make_fact(db, org.id)
        from backend.core.events import (
            FactChanged, get_event_bus, reset_event_bus,
            register_default_subscribers,
        )
        reset_event_bus()
        register_default_subscribers()
        seen = []
        get_event_bus().subscribe(FactChanged, seen.append)
        repo.transition(fact.id, org.id, "pending_review", operator_id="u1")
        assert len(seen) == 1
        assert seen[0].fact_id == fact.id
        assert seen[0].old_status == "draft"
        assert seen[0].new_status == "pending_review"


class TestContentVersion:
    def test_update_content_snapshots(self, db):
        org = _org(db)
        fact, repo = _make_fact(db, org.id, content="旧文案")
        v0 = fact.version
        fact = repo.update_content(fact.id, org.id, "u1", v0,
                                   content="新文案")
        assert fact.content == "新文案"
        assert fact.version == v0 + 1
        versions = repo.list_versions(fact.id, org.id)
        assert len(versions) == 1
        assert versions[0].content == "旧文案"
        assert versions[0].version_no == 1
        assert versions[0].created_by == "u1"

    def test_no_change_no_snapshot(self, db):
        org = _org(db)
        fact, repo = _make_fact(db, org.id, content="同文案")
        v0 = fact.version
        fact2 = repo.update_content(fact.id, org.id, "u1", v0,
                                    content="同文案")
        assert fact2.version == v0
        assert repo.list_versions(fact.id, org.id) == []

    def test_update_content_version_conflict(self, db):
        from backend.repositories.base import OptimisticLockError
        org = _org(db)
        fact, repo = _make_fact(db, org.id)
        with pytest.raises(OptimisticLockError):
            repo.update_content(fact.id, org.id, "u1", 999, content="x")


class TestExpireDue:
    def test_expire_due_sweeps_confirmed(self, db):
        org = _org(db)
        past = datetime.now(timezone.utc) - timedelta(days=1)
        future = datetime.now(timezone.utc) + timedelta(days=30)
        f1, repo = _make_fact(db, org.id, expires_at=past)
        f2, _ = _make_fact(db, org.id, expires_at=future)
        f3, _ = _make_fact(db, org.id)  # 无有效期
        for f in (f1, f2, f3):
            repo.transition(f.id, org.id, "pending_review", operator_id="u1")
            repo.transition(f.id, org.id, "confirmed", operator_id="u1",
                            reason="ok", expected_version=2)
        n = repo.expire_due(org.id)
        assert n == 1
        assert repo.get(f1.id, org.id).status == "expired"
        assert repo.get(f2.id, org.id).status == "confirmed"
        assert repo.get(f3.id, org.id).status == "confirmed"
        # 审计记了 system 自动过期
        hist = repo.list_history(f1.id, org.id)
        assert hist[0].to_status == "expired"
        assert hist[0].operator_id == "system"

    def test_list_usable_excludes_expired(self, db):
        org = _org(db)
        past = datetime.now(timezone.utc) - timedelta(days=1)
        f1, repo = _make_fact(db, org.id, expires_at=past)
        repo.transition(f1.id, org.id, "pending_review", operator_id="u1")
        repo.transition(f1.id, org.id, "confirmed", operator_id="u1",
                        reason="ok", expected_version=2)
        # 懒过滤：即使还没跑扫库，也查不到
        assert repo.list_usable(org.id) == []
        repo.expire_due(org.id)
        assert repo.list_usable(org.id) == []


# ================= Guard =================

def _confirm_fact(db, org_id, **kw):
    from backend.repositories.fact_repository import FactRepository
    repo = FactRepository(db)
    fact = repo.create(org_id=org_id, content=kw.pop("content", "x"), **kw)
    fact = repo.transition(fact.id, org_id, "pending_review", operator_id="u1")
    fact = repo.transition(fact.id, org_id, "confirmed", operator_id="u1",
                           reason="ok", expected_version=fact.version)
    return fact, repo


class TestGuard:
    def test_usable_fact_passes(self, db):
        org = _org(db)
        fact, _ = _confirm_fact(db, org.id)
        out = check_facts_usable(db, org.id, [fact.id])
        assert fact.id in out

    def test_missing_fact_rejected(self, db):
        org = _org(db)
        with pytest.raises(UnusableFactError) as e:
            check_facts_usable(db, org.id, ["no-such-id"])
        assert "no-such-id" in e.value.fact_ids

    def test_unconfirmed_rejected(self, db):
        org = _org(db)
        fact, _ = _make_fact(db, org.id)  # draft
        with pytest.raises(UnusableFactError):
            check_facts_usable(db, org.id, [fact.id])

    def test_revoked_rejected(self, db):
        org = _org(db)
        fact, repo = _confirm_fact(db, org.id)
        repo.transition(fact.id, org.id, "revoked", operator_id="u1",
                        reason="撤回", expected_version=fact.version)
        with pytest.raises(UnusableFactError):
            check_facts_usable(db, org.id, [fact.id])

    def test_expired_rejected(self, db):
        org = _org(db)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        fact, repo = _confirm_fact(db, org.id, expires_at=past)
        # 即使扫库还没跑，guard 也按 expires_at 拒绝
        with pytest.raises(UnusableFactError):
            check_facts_usable(db, org.id, [fact.id])

    def test_cross_org_rejected(self, db):
        org1 = _org(db, "组织A")
        org2 = _org(db, "组织B")
        fact, _ = _confirm_fact(db, org1.id)
        with pytest.raises(UnusableFactError):
            check_facts_usable(db, org2.id, [fact.id])

    def test_extract_fact_ids(self):
        assert extract_fact_ids(
            [{"id": "a"}, {"id": "b"}, {"id": "a"}, {"no_id": 1}, "c"]
        ) == ["a", "b", "c"]
        assert extract_fact_ids(None) == []
        assert extract_fact_ids([]) == []

    def test_assert_facts_usable_from_dicts(self, db):
        org = _org(db)
        fact, _ = _confirm_fact(db, org.id)
        out = assert_facts_usable(db, org.id, [{"id": fact.id, "content": "x"}])
        assert fact.id in out


# ================= API =================

@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """带认证覆盖的 TestClient：user=owner 角色。"""
    url = f"sqlite:///{tmp_path}/api.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    import backend.auth  # noqa: F401
    from backend.auth.database import reset_engine, get_session_factory, get_db
    from backend.auth.deps import get_current_user
    reset_engine()
    from alembic.config import Config
    from alembic import command
    cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    command.upgrade(cfg, "head")

    from backend.models import Organization, Membership
    from backend.auth.models import User as AuthUser
    db = get_session_factory()()
    # 建组织 + 用户 + owner 成员关系
    org = Organization(name="API测试社", created_by="u_owner")
    db.add(org)
    db.flush()
    user = AuthUser(id="u_owner", email="owner@test.local",
                    password_hash="x")
    db.add(user)
    db.flush()
    db.add(Membership(user_id="u_owner", org_id=org.id, role="owner"))
    db.commit()
    org_id = org.id
    db.close()

    from fastapi.testclient import TestClient
    from backend.app import app

    def _override_user():
        u = AuthUser(id="u_owner", email="owner@test.local",
                     password_hash="x")
        return u

    def _override_db():
        s = get_session_factory()()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app, raise_server_exceptions=False)
    yield client, org_id
    app.dependency_overrides.clear()
    reset_engine()


class TestFactAPI:
    def test_create_and_transition_flow(self, api_client):
        client, org_id = api_client
        # 创建（draft）
        r = client.post("/api/v1/facts", json={
            "org_id": org_id, "content": "章丘大葱，高 1.5 米",
            "source_type": "human_statement"})
        assert r.status_code == 201, r.text
        fact = r.json()["data"]
        assert fact["status"] == "draft"
        fid, ver = fact["id"], fact["version"]
        # 提交审核
        r = client.post(f"/api/v1/facts/{fid}/transition", json={
            "org_id": org_id, "target_status": "pending_review"})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["status"] == "pending_review"
        # 确认缺 reason → 400
        r = client.post(f"/api/v1/facts/{fid}/transition", json={
            "org_id": org_id, "target_status": "confirmed"})
        assert r.status_code == 400
        # 确认（带 reason）
        r = client.post(f"/api/v1/facts/{fid}/transition", json={
            "org_id": org_id, "target_status": "confirmed",
            "reason": "已核对", "evidence_ref": "e1"})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["status"] == "confirmed"
        assert data["allow_use"] is True
        # 审计
        r = client.get(f"/api/v1/facts/{fid}/history", params={"org_id": org_id})
        assert r.status_code == 200, r.text
        hist = r.json()["data"]
        assert len(hist) == 2
        assert hist[0]["to_status"] == "confirmed"
        assert hist[0]["reason"] == "已核对"
        # 非法流转 confirmed -> draft → 400
        r = client.post(f"/api/v1/facts/{fid}/transition", json={
            "org_id": org_id, "target_status": "draft"})
        assert r.status_code == 400

    def test_unauthorized_without_login(self, tmp_path, monkeypatch):
        """未登录访问 → 401（用干净 app，不覆盖依赖）。"""
        url = f"sqlite:///{tmp_path}/anon.db"
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
        import backend.auth  # noqa: F401
        from backend.auth.database import reset_engine, get_session_factory
        reset_engine()
        from alembic.config import Config
        from alembic import command
        cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
        command.upgrade(cfg, "head")
        from fastapi.testclient import TestClient
        from backend.app import app
        # 确保没有残留覆盖
        app.dependency_overrides.clear()
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/api/v1/facts", params={"org_id": "x"})
        assert r.status_code == 401, r.text
        reset_engine()
