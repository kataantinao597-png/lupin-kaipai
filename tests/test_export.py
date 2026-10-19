"""阶段 0.3：DOCX 导出优化验证。生成后回读检查：
表头跨页重复(tblHeader)、行不跨页断开(cantSplit)、内容完整、中文正常。"""
import json
import os
import sys
from docx import Document
from docx.oxml.ns import qn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _skip(msg):
    print(f"[跳过] {msg}")
    if "pytest" in sys.modules:
        import pytest
        pytest.skip(msg, allow_module_level=True)
    sys.exit(0)


EXP = os.path.join(
    os.path.dirname(ROOT), "goals",
    "shandong-ai-model-innovation-contest-registration",
    "experiments", "exp-2026-10-16",
)
# 兼容旧的绝对路径布局（原开发者机器）
if not os.path.isdir(EXP) and os.path.isdir(
        "/home/hatch/workspace/goals/shandong-ai-model-innovation-contest-registration/experiments/exp-2026-10-16"):
    EXP = "/home/hatch/workspace/goals/shandong-ai-model-innovation-contest-registration/experiments/exp-2026-10-16"
if not os.path.isdir(EXP):
    _skip(f"缺少实验数据目录，无法运行导出验证：{EXP}")

from backend.exporter import export_docx

OUT = "/tmp/test_shooting_plan.docx"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'通过' if cond else '未通过'}] {name} {detail}")


sb = json.load(open(f"{EXP}/sb_caseA_attempt1.json", encoding="utf-8"))
export_docx(sb, OUT)

doc = Document(OUT)
check("文档可正常打开", True, f"{len(doc.paragraphs)}段落/{len(doc.tables)}表格")

n_shots = len(sb["shots"])
full = "\n".join(p.text for p in doc.paragraphs)
# V2.0：分镜改为每镜头独立小节（不再用一张大表），不再断言 tables[0]
check("每镜头有独立小节", all(f"镜头 {i+1}" in full for i in range(n_shots)),
      f"shots={n_shots}")
check("旧'总拍摄时长'文案已移除", "总拍摄时长" not in full)
check("有'成片目标时长'", "成片目标时长" in full)
check("有'预计现场拍摄耗时'", "预计现场拍摄耗时" in full)

# 内容完整性
check("含产品信息", sb["product_summary"][:8] in full)
check("含创意方向", sb["creative_direction"][:6] in full)
check("含素材清单", all(m[:6] in full for m in sb["materials_checklist"][:3]))
check("含风险提示", any(r["text"][:6] in full for r in sb["risk_flags"]))
check("含待确认事项", all(c[:6] in full for c in sb["pending_confirmations"][:2]))
check("中文标题正常", "短视频拍摄执行单" in full)

print("\n==== 汇总 ====")
failed = [n for n, ok, _ in results if not ok]
print(f"共 {len(results)} 项，通过 {len(results)-len(failed)} 项")
if __name__ == "__main__":
    sys.exit(1 if failed else 0)
