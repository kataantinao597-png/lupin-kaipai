"""S6 审核闭环 API 回归测试：审核版本绑定、门禁配置、豁免、风险发现。

- 审核创建绑定内容哈希 + 规则/模型/Prompt 版本
- 内容变更后查询 latest 自动过期
- 审核状态流转（乐观锁）
- 风险发现导入 / 列表 / 处置
- 豁免创建 / 列表 / 撤销
- 门禁配置查询 / 调整
- v1 配置化交付检查
"""
import os
import sys

import pytest

pytestmark = pytest.mark.integration
from fastapi.testclient import TestClient

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from backend.app import app  # noqa: E402
from backend import models  # noqa: E402
from backend.auth.database import (  # noqa: E402
    get_engine,
    get_session_factory,
    reset_engine,
)
from backend.auth.deps import get_current_user  # noqa: E402
from backend.auth.models import User as AuthUser  # noqa: E402
from backend.core.events import reset_event_bus  # noqa: E402
from backend.repositories import StoryboardRepository  # noqa: E402


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """每个用例一个全新的 SQLite 文件库（alembic 建表到最新 head）。"""
    url = f"sqlite:///{tmp_path}/t.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    reset_engine()
    reset_event_bus()
    from alembic.config import Config
    from alembic import command
    cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    command.upgrade(cfg, "head")
    get_engine()  # 触发 ensure_indexes 等初始化
    from backend.models import Membership, Organization
    db = get_session_factory()()
    org = Organization(name="审核闭环测试社", created_by="u_owner")
    db.add(org)
    db.flush()
    user = AuthUser(id="u_owner", email="owner@test.local", password_hash="x")
    db.add(user)
    db.flush()
    db.add(Membership(user_id="u_owner", org_id=org.id, role="owner"))
    # 作品 + 分镜 + 镜头（审核内容哈希来源）
    work = models.Work(id="w1", org_id=org.id, owner_id="u_owner",
                       title="测试作品", data_json="{}")
    db.add(work)
    db.commit()
    srepo = StoryboardRepository(db)
    sb = srepo.create("w1", "分镜1")
    srepo.add_shot(sb.id, 1, {"text": "镜头1旁白"}, 5, shot_id_stable="shot-a")
    db.commit()
    org_id, work_id = org.id, work.id
    db.close()

    def _override_user():
        return AuthUser(id="u_owner", email="owner@test.local",
                        password_hash="x")

    def _override_db():
        s = get_session_factory()()
        try:
            yield s
        finally:
            s.close()

    from backend.auth.database import get_db as _get_db
    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[_get_db] = _override_db
    client = TestClient(app, raise_server_exceptions=False)
    yield client, org_id, work_id
    app.dependency_overrides.clear()
    reset_engine()
    reset_event_bus()


def _ok(r):
    assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"
    body = r.json()
    assert body["code"] == 0, f"code={body['code']}: {body}"
    return body["data"]


class TestReviewAPI:
    def test_create_binds_hash_and_versions(self, api_client):
        client, _, work_id = api_client
        data = _ok(client.post(f"/api/v1/works/{work_id}/reviews", json={}))
        assert len(data["content_hash"]) == 64
        assert data["rule_version"] == "1"
        assert data["prompt_version"] == "1"
        assert data["status"] == "pending"
        assert data["fresh"] is True

    def test_latest_auto_expires_on_content_change(self, api_client):
        client, _, work_id = api_client
        created = _ok(client.post(f"/api/v1/works/{work_id}/reviews", json={}))
        rid, ver = created["id"], created["version"]
        # 审核通过
        _ok(client.post(f"/api/v1/reviews/{rid}/transition", json={
            "to_status": "in_review", "expected_version": ver}))
        _ok(client.post(f"/api/v1/reviews/{rid}/transition", json={
            "to_status": "approved", "expected_version": ver + 1}))
        # 改分镜内容（走 repository，触发事件内联失效）
        db = get_session_factory()()
        srepo = StoryboardRepository(db)
        sb = srepo.list_by_work(work_id)[0]
        srepo.add_shot(sb.id, 2, {"text": "新镜头"}, 5,
                       shot_id_stable="shot-b")
        db.commit()
        db.close()
        # 查询 latest：应自动过期
        latest = _ok(client.get(f"/api/v1/works/{work_id}/reviews/latest"))
        assert latest["id"] == rid
        assert latest["fresh"] is False
        assert latest["status"] == "expired"
        assert "stale_reason" in latest

    def test_transition_version_conflict(self, api_client):
        client, _, work_id = api_client
        created = _ok(client.post(f"/api/v1/works/{work_id}/reviews", json={}))
        r = client.post(f"/api/v1/reviews/{created['id']}/transition", json={
            "to_status": "in_review", "expected_version": 999})
        assert r.status_code == 409

    def test_illegal_transition_rejected(self, api_client):
        client, _, work_id = api_client
        created = _ok(client.post(f"/api/v1/works/{work_id}/reviews", json={}))
        # pending 不能直接 approved
        r = client.post(f"/api/v1/reviews/{created['id']}/transition", json={
            "to_status": "approved", "expected_version": created["version"]})
        assert r.status_code == 422


class TestFindingAPI:
    def _mk_review(self, client, work_id):
        return _ok(client.post(f"/api/v1/works/{work_id}/reviews", json={}))

    def test_import_and_resolve(self, api_client):
        client, _, work_id = api_client
        # 给作品 data_json 塞 risk_flags
        db = get_session_factory()()
        work = db.query(models.Work).filter(
            models.Work.id == work_id).first()
        import json as _json
        work.data_json = _json.dumps({
            "storyboard": {"risk_flags": [
                {"text": "全网最低价", "level": "高", "reason": "绝对化用语",
                 "shot_number": 1, "source": "AI 风险识别"},
                {"text": "纯天然", "level": "低", "reason": "无事实依据",
                 "shot_number": 99},
            ]}})
        db.commit()
        db.close()
        rev = self._mk_review(client, work_id)
        data = _ok(client.post(
            f"/api/v1/reviews/{rev['id']}/findings/import"))
        assert data["total"] == 2
        by_sev = {f["severity"] for f in data["items"]}
        assert by_sev == {"high", "low"}
        # shot_number=1 应映射到稳定 ID shot-a
        first = [f for f in data["items"]
                 if f["text_fragment"] == "全网最低价"][0]
        assert first["shot_id"] == "shot-a"
        assert first["shot_no"] == 1
        # 列表 + open 计数
        lst = _ok(client.get(f"/api/v1/reviews/{rev['id']}/findings"))
        assert lst["open_count"] == 2
        # 处置
        fixed = _ok(client.patch(
            f"/api/v1/risk-findings/{first['id']}",
            json={"to_status": "fixed"}))
        assert fixed["status"] == "fixed"
        assert fixed["resolved_by"] == "u_owner"
        lst2 = _ok(client.get(
            f"/api/v1/reviews/{rev['id']}/findings",
            params={"status": "open"}))
        assert lst2["total"] == 1


class TestWaiverAPI:
    def test_waiver_flow(self, api_client):
        client, _, work_id = api_client
        # final/shooting 可豁免
        w = _ok(client.post(f"/api/v1/works/{work_id}/waivers", json={
            "gate_type": "final", "check_id": "shooting",
            "reason": "场地延期"}))
        assert w["approved_by"] == "u_owner"
        lst = _ok(client.get(f"/api/v1/works/{work_id}/waivers"))
        assert lst["total"] == 1
        # 不可豁免项
        r = client.post(f"/api/v1/works/{work_id}/waivers", json={
            "gate_type": "final", "check_id": "product", "reason": "x"})
        assert r.status_code == 422
        # 撤销
        _ok(client.delete(f"/api/v1/waivers/{w['id']}"))
        lst2 = _ok(client.get(f"/api/v1/works/{work_id}/waivers"))
        assert lst2["total"] == 0


class TestGateAPI:
    def test_get_and_patch(self, api_client):
        client, _, _ = api_client
        data = _ok(client.get("/api/v1/delivery/gates/plan"))
        assert len(data["items"]) == 7
        # 调整
        cfg = _ok(client.patch("/api/v1/delivery/gates/plan", json={
            "check_id": "facts", "is_blocking": True}))
        assert cfg["is_blocking"] is True
        assert cfg["updated_by"] == "u_owner"
        # 改回去
        _ok(client.patch("/api/v1/delivery/gates/plan", json={
            "check_id": "facts", "is_blocking": False}))

    def test_v1_delivery_check(self, api_client):
        client, _, work_id = api_client
        data = _ok(client.post(f"/api/v1/works/{work_id}/delivery-check",
                               json={
                                   "check_type": "plan",
                                   "product": {"product_name": "章丘大葱"},
                                   "storyboard": {"shots": [{"a": 1}]},
                               }))
        assert "ready" in data and "items" in data
        assert data["check_type"] == "plan"
        # 豁免参与计算：先豁免 final/shooting，再查 final
        _ok(client.post(f"/api/v1/works/{work_id}/waivers", json={
            "gate_type": "final", "check_id": "shooting",
            "reason": "测试豁免"}))
        data2 = _ok(client.post(
            f"/api/v1/works/{work_id}/delivery-check",
            json={"check_type": "final",
                  "product": {"product_name": "章丘大葱"},
                  "storyboard": {"shots": [{"a": 1}]}}))
        shooting = [i for i in data2["items"] if i["id"] == "shooting"][0]
        assert shooting.get("waived") is True
