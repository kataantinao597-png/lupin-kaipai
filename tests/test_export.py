"""DOCX 导出验证（拍摄执行单）。

契约已随导出器 V2.0 变更：分镜不再是单张大表，而是"五章 + 每镜头独立小节"。
原阶段 0.3 脚本版断言的是旧表格格式（doc.tables[0]），已于 2026-10-20 同步审阅包时
改写为 pytest 版并按现行格式断言；产品代码未动。
"""
import json
import os
import sys

import pytest
from docx import Document

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

EXP = os.path.join(
    os.path.dirname(ROOT), "goals",
    "shandong-ai-model-innovation-contest-registration",
    "experiments", "exp-2026-10-16",
)
# 兼容旧的绝对路径布局（原开发者机器）
if not os.path.isdir(EXP) and os.path.isdir(
        "/home/hatch/workspace/goals/shandong-ai-model-innovation-contest-registration/experiments/exp-2026-10-16"):
    EXP = "/home/hatch/workspace/goals/shandong-ai-model-innovation-contest-registration/experiments/exp-2026-10-16"

pytestmark = pytest.mark.skipif(
    not os.path.isdir(EXP), reason=f"缺少实验数据目录：{EXP}")


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    from backend.exporter import export_docx
    sb = json.load(open(os.path.join(EXP, "sb_caseA_attempt1.json"), encoding="utf-8"))
    out = str(tmp_path_factory.mktemp("export") / "shooting_plan.docx")
    export_docx(sb, out)
    return sb, Document(out)


def _headings(doc, level):
    return [p.text for p in doc.paragraphs if p.style.name == f"Heading {level}"]


def test_doc_opens_and_has_five_chapters(exported):
    _, doc = exported
    h1 = _headings(doc, 1)
    for chapter in ("一、任务概要", "二、分镜表", "三、素材准备清单",
                    "四、宣传事实风险提示", "五、用户待确认事项"):
        assert chapter in h1, f"缺章节：{chapter}"


def test_one_section_per_shot(exported):
    sb, doc = exported
    shot_heads = [t for t in _headings(doc, 2) if t.startswith("镜头 ")]
    assert len(shot_heads) == len(sb["shots"])


def test_content_completeness(exported):
    sb, doc = exported
    full = "\n".join(p.text for p in doc.paragraphs)
    assert sb["product_summary"][:8] in full
    assert sb["creative_direction"][:6] in full
    assert all(m[:6] in full for m in sb["materials_checklist"][:3])
    assert any(r["text"][:6] in full for r in sb["risk_flags"])
    assert all(c[:6] in full for c in sb["pending_confirmations"][:2])
    assert "短视频拍摄执行单" in full
    assert "成片目标时长" in full
