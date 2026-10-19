"""V2.1：RiskFlag 扩展 / Fact.scope / 时长容差配置化 / 首尾镜头结构 warning。"""
import os
import sys

import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import backend.auth  # noqa: F401,E402  先初始化包顺序
from backend.schemas import RiskFlag  # noqa: E402
from backend import risk as risk_mod  # noqa: E402
from backend.validator import validate_dict, validate_warnings  # noqa: E402
from backend.core.config import get_settings  # noqa: E402


def _sb(shots, target=None):
    total = sum(s.get("final_duration_seconds") or s.get("duration_seconds", 0)
                for s in shots)
    return {
        "product_summary": "烟台大樱桃",
        "target_audience": "消费者",
        "creative_direction": "果园来信",
        "target_duration_seconds": total if target is None else target,
        "shoot": {"device": "手机", "people": 1, "lighting": "自然光",
                  "location": "果园", "target_duration_seconds": 30},
        "shots": shots,
        "materials_checklist": [], "risk_flags": [], "pending_confirmations": [],
    }


def _shot(n, **kw):
    base = {
        "shot_number": n, "duration_seconds": 5, "shot_type": "远景",
        "scene_description": "果园", "shooting_action": "手持拍摄5秒",
        "camera_movement": "固定机位",
        "voiceover": "看看樱桃", "subtitle": "樱桃",
        "props": [], "materials": [], "fact_refs": [],
    }
    base.update(kw)
    return base


# ---------- 任务1：RiskFlag ----------

class TestRiskFlag:
    def test_extended_fields(self):
        f = RiskFlag(text="最甜", level="中", reason="极限词",
                     shot_number=2, linked_text="这是最甜的樱桃",
                     suggestion="改为：口感清甜", status="open")
        assert f.shot_number == 2
        assert f.linked_text == "这是最甜的樱桃"
        assert f.suggestion == "改为：口感清甜"
        assert f.status == "open"

    def test_backward_compatible(self):
        f = RiskFlag(text="x", level="低")
        assert f.shot_number is None
        assert f.linked_text == ""
        assert f.suggestion == ""
        assert f.status == "open"

    def test_bad_status_rejected(self):
        with pytest.raises(Exception):
            RiskFlag(text="x", level="低", status="done")

    def test_grounding_flags_carry_shot_number(self):
        sb = _sb([_shot(1, fact_refs=["nope"],
                        voiceover="绝对最甜，不甜不要钱")])
        facts = [{"id": "f1", "status": "confirmed", "allow_use": True}]
        flags = risk_mod.check_fact_grounding(sb, facts)
        assert flags, "应检出引用了不存在的事实ID"
        assert all("shot_number" in f and f["shot_number"] == 1 for f in flags)

    def test_grounding_risky_claim_has_linked_text(self):
        sb = _sb([_shot(1, voiceover="全网最低价，错过不再有")])
        flags = risk_mod.check_fact_grounding(sb, [])
        assert flags
        assert flags[0]["linked_text"], "高风险宣称应带上关联原文"

    def test_rule_scan_parses_shot_number(self):
        flags = risk_mod.rule_scan("这是最便宜的", "镜头3字幕")
        assert flags
        assert flags[0]["shot_number"] == 3
        assert "最便宜" in flags[0]["linked_text"]

    def test_rule_scan_no_shot_number(self):
        flags = risk_mod.rule_scan("最便宜", "用户输入卖点")
        assert flags
        assert flags[0]["shot_number"] is None


# ---------- 任务2：Fact.scope ----------

@pytest.fixture()
def db(tmp_path, monkeypatch):
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


@pytest.mark.integration
class TestFactScope:
    def test_create_and_read_scope(self, db):
        from backend.models import Organization
        from backend.repositories.fact_repository import FactRepository
        org = Organization(name=" scope合作社", created_by="u1")
        db.add(org)
        db.commit()
        db.refresh(org)
        repo = FactRepository(db)
        fact = repo.create(org_id=org.id, content="烟台大樱桃",
                           scope="仅烟台产地款")
        assert fact.scope == "仅烟台产地款"
        got = repo.get(fact.id, org.id)
        assert got.scope == "仅烟台产地款"

    def test_scope_defaults_empty(self, db):
        from backend.models import Organization
        from backend.repositories.fact_repository import FactRepository
        org = Organization(name=" scope合作社2", created_by="u1")
        db.add(org)
        db.commit()
        db.refresh(org)
        repo = FactRepository(db)
        fact = repo.create(org_id=org.id, content="无范围事实")
        assert fact.scope == ""


# ---------- 任务3：容差配置化 + 首尾 warning ----------

class TestDurationTolerance:
    def _sb_total(self, total):
        # 6 镜，每镜 total/6；目标固定 30s，用于测容差
        per = total / 6
        return _sb([_shot(i + 1, duration_seconds=per,
                          final_duration_seconds=per) for i in range(6)],
                   target=30)

    def test_default_tolerance_2s(self):
        get_settings.cache_clear()
        try:
            assert get_settings().LUPIN_DURATION_TOLERANCE == 2.0
            errors, _ = validate_dict(self._sb_total(31))  # 误差 1s
            assert not [e for e in errors if "总成片时长" in e]
            errors, _ = validate_dict(self._sb_total(33))  # 误差 3s
            assert any("总成片时长" in e for e in errors)
        finally:
            get_settings.cache_clear()

    def test_env_overrides_tolerance(self, monkeypatch):
        monkeypatch.setenv("LUPIN_DURATION_TOLERANCE", "0.5")
        get_settings.cache_clear()
        try:
            assert get_settings().LUPIN_DURATION_TOLERANCE == 0.5
            errors, _ = validate_dict(self._sb_total(31))  # 误差 1s > 0.5
            assert any("总成片时长" in e for e in errors)
            assert any("0.5s" in e for e in errors)
        finally:
            get_settings.cache_clear()


class TestStructureWarnings:
    def test_first_shot_not_wide_warns(self):
        sb = _sb([_shot(1, shot_type="特写"), _shot(2, shot_type="近景",
                       voiceover="谢谢观看，欢迎下单")])
        errors, parsed = validate_dict(sb)
        assert not errors
        warns = validate_warnings(parsed)
        assert any("首镜" in w for w in warns)

    def test_first_shot_wide_no_warning(self):
        sb = _sb([_shot(1, shot_type="远景"), _shot(2, shot_type="近景",
                       voiceover="谢谢观看，欢迎下单")])
        errors, parsed = validate_dict(sb)
        assert not errors
        warns = validate_warnings(parsed)
        assert not any("首镜" in w for w in warns)

    def test_last_shot_no_closing_warns(self):
        sb = _sb([_shot(1, shot_type="远景"),
                  _shot(2, shot_type="近景", voiceover="樱桃真红")])
        errors, parsed = validate_dict(sb)
        assert not errors
        warns = validate_warnings(parsed)
        assert any("收尾" in w for w in warns)

    def test_last_shot_closing_no_warning(self):
        sb = _sb([_shot(1, shot_type="远景"),
                  _shot(2, shot_type="近景", voiceover="喜欢就下单吧")])
        errors, parsed = validate_dict(sb)
        assert not errors
        warns = validate_warnings(parsed)
        assert not any("收尾" in w for w in warns)

    def test_warnings_do_not_block(self):
        # warning 不阻断：validate_dict 依然通过
        sb = _sb([_shot(1, shot_type="特写"),
                  _shot(2, shot_type="近景", voiceover="樱桃真红")])
        errors, parsed = validate_dict(sb)
        assert not errors
        assert len(validate_warnings(parsed)) >= 2
