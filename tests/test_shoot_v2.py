"""V2.0 P1：shoot_v2 接口测试（验收写入 / 补拍清单 / 剪辑交付清单 / DOCX）。

≥8 用例：400（非法状态/空shot_key/不存在素材/跨组织素材）、
跨组织作品 404、补拍清单只含 needs_reshoot、剪辑清单时长求和、
v1 老作品不报错、DOCX 下载。
"""
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 先 import backend.auth，保证包初始化顺序（避开循环导入）
import backend.auth  # noqa: F401,E402


def _shots_v2():
    return [
        {"shot_number": 1, "shot_purpose": "开场环境",
         "record_duration_seconds": 8, "usable_duration_seconds": 6,
         "final_duration_seconds": 5, "duration_seconds": 5,
         "voiceover": "旁白一", "subtitle": "字幕一",
         "transition": "淡入", "edit_notes": "剪辑备注一",
         "reshoot_guide": "重拍指引一",
         "failure_modes": [{"symptom": "抖", "fix": "用支架"}]},
        {"shot_number": 2, "shot_purpose": "产品特写",
         "record_duration_seconds": 10, "usable_duration_seconds": 7,
         "final_duration_seconds": 5, "duration_seconds": 5,
         "voiceover": "旁白二", "subtitle": "字幕二"},
        {"shot_number": 3, "shot_purpose": "收尾",
         "record_duration_seconds": 6, "usable_duration_seconds": 4,
         "final_duration_seconds": 5, "duration_seconds": 5,
         "voiceover": "旁白三", "subtitle": "字幕三"},
    ]


def _shots_v1():
    # v1 老数据：只有 duration_seconds，无新字段
    return [
        {"shot_number": 1, "duration_seconds": 5, "scene_description": "果园远景",
         "voiceover": "老旁白一", "subtitle": "老字幕一"},
        {"shot_number": 2, "duration_seconds": 5, "scene_description": "果实特写",
         "voiceover": "老旁白二", "subtitle": "老字幕二"},
    ]


def _shots_simp():
    # B-01：one-shot 简化分镜——只有 shot_no / duration_sec
    return [
        {"shot_no": 1, "duration_sec": 12,
         "voiceover": "脆甜", "subtitle": "脆甜"},
        {"shot_no": 2, "duration_sec": 8,
         "voiceover": "现摘", "subtitle": "现摘"},
    ]


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """带认证覆盖的 TestClient：u_owner 为 org1 的 owner；另建 org2 做隔离测试。"""
    url = f"sqlite:///{tmp_path}/api.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    from backend.auth.database import reset_engine, get_session_factory, get_db
    from backend.auth.deps import get_current_user
    reset_engine()
    from alembic.config import Config
    from alembic import command
    cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    command.upgrade(cfg, "head")

    from backend.models import Organization, Membership, Work, Asset
    from backend.auth.models import User as AuthUser
    db = get_session_factory()()

    org1 = Organization(name="P1测试社", created_by="u_owner")
    org2 = Organization(name="隔离社", created_by="u_other")
    db.add_all([org1, org2])
    db.flush()
    user = AuthUser(id="u_owner", email="owner@test.local", password_hash="x")
    db.add(user)
    db.flush()
    db.add(Membership(user_id="u_owner", org_id=org1.id, role="owner"))
    db.commit()

    def _mk_work(org_id, shots, title="樱桃测试"):
        w = Work(org_id=org_id, owner_id="u_owner", title=title,
                 data_json=json.dumps(
                     {"storyboard": {"shots": shots},
                      "product": {"product_name": "烟台大樱桃"}},
                     ensure_ascii=False))
        db.add(w)
        db.flush()
        return w.id

    work_v2 = _mk_work(org1.id, _shots_v2(), "V2作品")
    work_v1 = _mk_work(org1.id, _shots_v1(), "V1老作品")
    work_simp = _mk_work(org1.id, _shots_simp(), "简化分镜作品")
    work_org2 = _mk_work(org2.id, _shots_v1(), "隔离作品")

    asset = Asset(org_id=org1.id, work_id=work_v2, filename="a.png",
                  mime="image/png", size=10,
                  key=f"{org1.id}/{work_v2}/k1_a.png")
    db.add(asset)
    db.flush()
    asset_org2 = Asset(org_id=org2.id, work_id=work_org2, filename="b.png",
                       mime="image/png", size=10,
                       key=f"{org2.id}/{work_org2}/k2_b.png")
    db.add(asset_org2)
    db.commit()
    ids = {"org1": org1.id, "org2": org2.id, "work_v2": work_v2,
           "work_v1": work_v1, "work_simp": work_simp,
           "work_org2": work_org2,
           "asset1": asset.id, "asset_org2": asset_org2.id}
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
    # 限流中间件按 (IP + 鉴权头哈希) 分桶：带固定测试头避免与全量跑时
    # 其他模块共享 testclient 匿名桶（100次/分钟）而误触 429。
    # 真实前端同样带 Authorization 头，按用户分桶，此处行为一致。
    client.headers.update({"Authorization": "Bearer shoot-v2-test-token"})
    yield client, ids
    app.dependency_overrides.clear()


def _accept(client, wid, **kw):
    body = {"shot_key": "s1_abc", "shot_number": 1, "status": "accepted",
            "note": "", "asset_ids": []}
    body.update(kw)
    return client.patch(f"/api/v2/shoot/works/{wid}/shots/accept", json=body)


def _shoot_flow(client, wid, shot_key, shot_number, target="captured_pending_review"):
    """V2.1：把镜头拍摄状态沿合法转换链推到目标态。"""
    chain = {"shooting": ["shooting"],
             "captured_pending_review": ["shooting", "captured_pending_review"],
             "passed": ["shooting", "captured_pending_review", "passed"],
             "substituted": ["substituted"]}[target]
    for st in chain:
        kw = {"shoot_status": st}
        if st == "substituted":
            kw["note"] = "现场无果园，改室内摆拍"
        r = _accept(client, wid, shot_key=shot_key, shot_number=shot_number,
                    status="pending", **kw)
        assert r.status_code == 200, r.text
    return chain


# ---------- 验收写入 ----------

def test_accept_ok_writes_record(api_client):
    client, ids = api_client
    # V2.1：先走拍摄流到 captured_pending_review，才能验收通过
    _shoot_flow(client, ids["work_v2"], "s1_abc", 1)
    r = _accept(client, ids["work_v2"], shot_key="s1_abc", shot_number=1,
                status="accepted", note="画面清晰",
                asset_ids=[ids["asset1"]])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["shot_key"] == "s1_abc"
    assert d["acceptance"]["status"] == "accepted"
    assert d["acceptance"]["asset_ids"] == [ids["asset1"]]
    assert d["shoot_status"] == "captured_pending_review"


def test_accept_invalid_status_400(api_client):
    client, ids = api_client
    r = _accept(client, ids["work_v2"], status="bogus")
    assert r.status_code == 400


def test_accept_empty_shot_key_400(api_client):
    client, ids = api_client
    r = _accept(client, ids["work_v2"], shot_key="  ")
    assert r.status_code == 400


def test_accept_unknown_asset_400(api_client):
    client, ids = api_client
    r = _accept(client, ids["work_v2"], asset_ids=["does_not_exist"])
    assert r.status_code == 400


def test_accept_other_org_asset_400(api_client):
    client, ids = api_client
    r = _accept(client, ids["work_v2"], asset_ids=[ids["asset_org2"]])
    assert r.status_code == 400


def test_cross_org_work_404(api_client):
    client, ids = api_client
    r = _accept(client, ids["work_org2"], status="accepted")
    assert r.status_code == 404  # 防枚举：无权作品统一 404
    r = client.get(f"/api/v2/shoot/works/{ids['work_org2']}/edit-list")
    assert r.status_code == 404


# ---------- 补拍清单 ----------

def test_reshoot_list_only_needs_reshoot(api_client):
    client, ids = api_client
    _shoot_flow(client, ids["work_v2"], "s1_a", 1)
    _accept(client, ids["work_v2"], shot_key="s1_a", shot_number=1,
            status="accepted")
    _accept(client, ids["work_v2"], shot_key="s2_b", shot_number=2,
            status="needs_reshoot", note="主体模糊")
    _accept(client, ids["work_v2"], shot_key="s3_c", shot_number=3,
            status="pending")
    r = client.get(f"/api/v2/shoot/works/{ids['work_v2']}/reshoot-list")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    it = items[0]
    assert it["shot_number"] == 2
    assert it["note"] == "主体模糊"
    # shot 2 的分镜无 reshoot_guide → null，不编造
    assert it["reshoot_guide"] is None
    assert it["shot_purpose"] == "产品特写"


def test_reshoot_list_empty_not_faked(api_client):
    client, ids = api_client
    _shoot_flow(client, ids["work_v2"], "s1_a", 1)
    _accept(client, ids["work_v2"], shot_key="s1_a", shot_number=1,
            status="accepted")
    r = client.get(f"/api/v2/shoot/works/{ids['work_v2']}/reshoot-list")
    assert r.status_code == 200
    # 空就是空，不得伪造"全部通过"之类文案
    assert r.json()["items"] == []


# ---------- 剪辑交付清单 ----------

def test_edit_list_sums_and_four_durations(api_client):
    client, ids = api_client
    _shoot_flow(client, ids["work_v2"], "s1_a", 1)
    _accept(client, ids["work_v2"], shot_key="s1_a", shot_number=1,
            status="accepted", asset_ids=[ids["asset1"]])
    _accept(client, ids["work_v2"], shot_key="s2_b", shot_number=2,
            status="needs_reshoot")
    r = client.get(f"/api/v2/shoot/works/{ids['work_v2']}/edit-list")
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["items"]) == 3
    s = d["summary"]
    assert s["total_final_seconds"] == 15
    assert s["total_record_seconds"] == 24  # 8+10+6
    assert s["accepted_count"] == 1
    assert s["total_count"] == 3
    assert s["missing_shots"] == [2, 3]
    assert len(s["pre_publish_checklist"]) == 4
    # 时长四分法字段齐全
    it1 = d["items"][0]
    for k in ("record_seconds", "raw_asset_seconds",
              "usable_seconds", "final_seconds"):
        assert k in it1
    assert it1["record_seconds"] == 8
    assert it1["usable_seconds"] == 6
    assert it1["final_seconds"] == 5
    # 素材 meta 无 duration 数据 → null，不编造
    assert it1["raw_asset_seconds"] is None
    assert it1["acceptance_status"] == "accepted"
    assert it1["missing"] is False
    # 未写入验收的镜头 → pending（需要人工确认）
    assert d["items"][2]["acceptance_status"] == "pending"
    assert d["items"][2]["missing"] is True


def test_edit_list_v1_legacy_no_crash(api_client):
    client, ids = api_client
    r = client.get(f"/api/v2/shoot/works/{ids['work_v1']}/edit-list")
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["items"]) == 2
    # v1 无新字段：final 回退 duration_seconds；其余 null
    assert d["items"][0]["final_seconds"] == 5
    assert d["items"][0]["record_seconds"] is None
    assert d["items"][0]["acceptance_status"] == "pending"
    assert d["summary"]["total_final_seconds"] == 10
    assert d["summary"]["missing_shots"] == [1, 2]


def test_edit_list_simplified_shots_b01(api_client):
    """B-01：one-shot 简化分镜（shot_no/duration_sec）在剪辑清单与
    时间轴里与 v1 格式等价：镜号不为 None、总时长 20s。"""
    client, ids = api_client
    r = client.get(f"/api/v2/shoot/works/{ids['work_simp']}/edit-list")
    assert r.status_code == 200, r.text
    d = r.json()
    assert [i["shot_number"] for i in d["items"]] == [1, 2]
    assert d["items"][0]["final_seconds"] == 12
    assert d["items"][1]["final_seconds"] == 8
    assert d["summary"]["total_final_seconds"] == 20
    assert d["summary"]["missing_shots"] == [1, 2]
    # 时间轴回退分支同样认 duration_sec
    t = client.get(f"/api/v1/works/{ids['work_simp']}/timeline")
    assert t.status_code == 200, t.text
    td = t.json()["data"]
    assert td["total_duration_seconds"] == 20
    assert [s["shot_no"] for s in td["shots"]] == [1, 2]


def test_edit_list_docx_200(api_client):
    client, ids = api_client
    r = client.get(f"/api/v2/shoot/works/{ids['work_v2']}/edit-list.docx")
    assert r.status_code == 200, r.text[:200]
    assert "wordprocessingml" in r.headers["content-type"]
    assert r.content[:2] == b"PK"  # zip = docx
    assert len(r.content) > 3000


# ---------- V2.1 状态机 ----------

def test_shoot_illegal_transition_400(api_client):
    client, ids = api_client
    # pending 不能直达 passed
    r = _accept(client, ids["work_v2"], shot_key="s9_x", shot_number=1,
                status="pending", shoot_status="passed")
    assert r.status_code == 400, r.text
    assert "不能从 pending 直接到 passed" in r.text


def test_shoot_substituted_requires_note_400(api_client):
    client, ids = api_client
    r = _accept(client, ids["work_v2"], shot_key="s9_x", shot_number=1,
                status="pending", shoot_status="substituted", note="  ")
    assert r.status_code == 400, r.text


def test_shoot_substituted_with_note_ok(api_client):
    client, ids = api_client
    r = _accept(client, ids["work_v2"], shot_key="s9_x", shot_number=1,
                status="pending", shoot_status="substituted",
                note="无果园，改室内摆拍")
    assert r.status_code == 200, r.text
    assert r.json()["shoot_status"] == "substituted"


def test_accept_requires_shoot_done_400(api_client):
    client, ids = api_client
    # 无拍摄记录（缺省 pending）直接验收 → 400
    r = _accept(client, ids["work_v2"], shot_key="s9_x", shot_number=1,
                status="accepted")
    assert r.status_code == 400, r.text
    assert "请先完成拍摄再验收" in r.text


def test_accept_after_shoot_ok(api_client):
    client, ids = api_client
    _shoot_flow(client, ids["work_v2"], "s9_x", 1)
    r = _accept(client, ids["work_v2"], shot_key="s9_x", shot_number=1,
                status="accepted")
    assert r.status_code == 200, r.text
    assert r.json()["acceptance"]["status"] == "accepted"


def test_legal_rework_chain(api_client):
    """passed→rework→shooting→captured_pending_review→accepted 合法链走通。"""
    client, ids = api_client
    wid = ids["work_v2"]
    # 拍完并验收通过
    _shoot_flow(client, wid, "s9_x", 1, target="passed")
    r = _accept(client, wid, shot_key="s9_x", shot_number=1, status="accepted")
    assert r.status_code == 200, r.text
    # 发现问题 → 需要补拍（验收态）
    r = _accept(client, wid, shot_key="s9_x", shot_number=1,
                status="needs_reshoot", note="逆光过曝")
    assert r.status_code == 200, r.text
    # 拍摄态：passed → rework → shooting → captured_pending_review
    for st in ("rework", "shooting", "captured_pending_review"):
        r = _accept(client, wid, shot_key="s9_x", shot_number=1,
                    status="needs_reshoot", shoot_status=st)
        assert r.status_code == 200, (st, r.text)
    # 重新验收通过
    r = _accept(client, wid, shot_key="s9_x", shot_number=1, status="accepted")
    assert r.status_code == 200, r.text
    assert r.json()["shoot_status"] == "captured_pending_review"


def test_legacy_shoot_state_mapping(api_client):
    """旧版前端五态值（shot=已拍待验收）兼容：可直接验收。"""
    client, ids = api_client
    wid = ids["work_v2"]
    # 直接写一条旧格式拍摄状态（模拟历史数据）
    from backend.models import Work
    from backend.auth.database import get_session_factory
    db = get_session_factory()()
    try:
        w = db.query(Work).filter(Work.id == wid).first()
        data = json.loads(w.data_json)
        data["shoot_state"] = {"s9_x": {"status": "shot", "note": ""}}
        w.data_json = json.dumps(data, ensure_ascii=False)
        db.commit()
    finally:
        db.close()
    r = _accept(client, wid, shot_key="s9_x", shot_number=1, status="accepted")
    assert r.status_code == 200, r.text
    assert r.json()["shoot_status"] == "captured_pending_review"
