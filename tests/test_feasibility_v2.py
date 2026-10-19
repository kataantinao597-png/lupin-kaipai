"""V2.0 P0：feasibility_v2 与风险文案常量测试。"""
import sys, os
sys.path.insert(0, os.path.expanduser("~/workspace/lupin-kaipai"))

from backend.feasibility_v2 import check_feasibility, READY, NEED_CONFIRM, NEED_ALTERNATIVE
from backend.risk import NO_RISK_FOUND_TEXT


def test_no_risk_text_holds_compliance_line():
    # 必须守住"不把未发现风险表述为已保证合规"这条线
    assert "不保证广告合规" in NO_RISK_FOUND_TEXT
    assert "人工确认" in NO_RISK_FOUND_TEXT
    for bad in ["保证合规", "已合规", "符合广告法", "无风险"]:
        assert bad not in NO_RISK_FOUND_TEXT, f"文案不应包含: {bad}"


def test_no_orchard_gives_alternative_not_rejection():
    """无果园只有一箱网购樱桃：必须返回 need_alternative + 具体替代路线，而不是拒单。"""
    r = check_feasibility(
        {"product_name": "烟台大樱桃", "description": "网购的一箱樱桃"},
        {"device": "手机", "people": 1, "lighting": "自然光", "location": "室内",
         "available_resources": ["手机"], "unavailable_scenes": ["果园"],
         "skill_level": "beginner"},
        "商品销售",
    )
    assert r["status"] == NEED_ALTERNATIVE
    orchard = [c for c in r["checks"] if "果园" in c["item"]]
    assert len(orchard) == 1
    assert orchard[0]["status"] == NEED_ALTERNATIVE
    alt = orchard[0]["alternative"]
    assert alt, "need_alternative 必须给出替代路线"
    assert "拒" not in alt and "无法" not in alt.replace("无法按原样", "")
    assert "网购" in alt or "室内" in alt


def test_all_ready_when_conditions_met():
    r = check_feasibility(
        {"product_name": "烟台大樱桃"},
        {"device": "手机", "people": 2, "lighting": "自然光", "location": "果园，室内有桌",
         "available_resources": ["手机", "手机支架", "补光灯", "背景布"],
         "unavailable_scenes": [], "skill_level": "advanced"},
        "品牌宣传",
    )
    assert r["status"] == READY
    assert all(c["status"] == READY for c in r["checks"])


def test_orchard_location_leaves_indoor_table_unconfirmed():
    """在果园时，室内桌面不应被默认假定为可用（防审计指出的问题）。"""
    r = check_feasibility(
        {"product_name": "烟台大樱桃"},
        {"device": "手机", "people": 1, "lighting": "自然光", "location": "果园",
         "available_resources": ["手机", "手机支架"],
         "unavailable_scenes": [], "skill_level": "advanced"},
        "品牌宣传",
    )
    table = [c for c in r["checks"] if "室内桌面" in c["item"]]
    assert table and table[0]["status"] == NEED_CONFIRM


def test_beginner_gets_simplified_alternative():
    r = check_feasibility(
        {"product_name": "德州扒鸡"},
        {"device": "手机", "people": 1, "lighting": "自然光", "location": "家里",
         "available_resources": ["手机"], "unavailable_scenes": [],
         "skill_level": "beginner"},
        "商品销售",
    )
    skill = [c for c in r["checks"] if "熟练度" in c["item"]]
    assert skill and skill[0]["status"] == NEED_CONFIRM
    assert "固定机位" in skill[0]["alternative"]


def test_output_shape():
    r = check_feasibility({}, {}, "")
    assert set(r.keys()) == {"status", "checks"}
    assert r["status"] in (READY, NEED_CONFIRM, NEED_ALTERNATIVE)
    for c in r["checks"]:
        assert set(c.keys()) == {"item", "status", "message", "alternative"}
        assert c["status"] in (READY, NEED_CONFIRM, NEED_ALTERNATIVE)
