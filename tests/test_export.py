"""阶段 0.3：DOCX 导出优化验证。生成后回读检查：
表头跨页重复(tblHeader)、行不跨页断开(cantSplit)、内容完整、中文正常。"""
import json
import sys
from docx import Document
from docx.oxml.ns import qn

sys.path.insert(0, "/home/hatch/workspace/lupin-kaipai")

from backend.exporter import export_docx

EXP = "/home/hatch/workspace/goals/shandong-ai-model-innovation-contest-registration/experiments/exp-2026-10-16"
OUT = "/tmp/test_shooting_plan.docx"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'通过' if cond else '未通过'}] {name} {detail}")


sb = json.load(open(f"{EXP}/sb_caseA_attempt1.json", encoding="utf-8"))
export_docx(sb, OUT)

doc = Document(OUT)
check("文档可正常打开", True, f"{len(doc.paragraphs)}段落/{len(doc.tables)}表格")

table = doc.tables[0]
n_shots = len(sb["shots"])
check("分镜表行数=表头+镜头数", len(table.rows) == n_shots + 1,
      f"rows={len(table.rows)}, shots={n_shots}")
check("表头6列完整", len(table.rows[0].cells) == 6)

# tblHeader 检查
hdr_tcPr = table.rows[0].cells[0]._tc.get_or_add_tcPr()
has_tblHeader = hdr_tcPr.find(qn("w:tblHeader")) is not None
check("表头行跨页重复(tblHeader)", has_tblHeader)

# cantSplit 检查（抽查所有行）
def row_cantsplit(row):
    trPr = row._tr.find(qn("w:trPr"))
    if trPr is None:
        return False
    el = trPr.find(qn("w:cantSplit"))
    return el is not None and el.get(qn("w:val"), "true") == "true"

all_rows = all(row_cantsplit(r) for r in table.rows)
check("所有行不跨页断开(cantSplit)", all_rows)

# 内容完整性
full = "\n".join(p.text for p in doc.paragraphs)
check("含产品信息", sb["product_summary"][:8] in full)
check("含创意方向", sb["creative_direction"][:6] in full)
check("含素材清单", all(m[:6] in full for m in sb["materials_checklist"][:3]))
check("含风险提示", any(r["text"][:6] in full for r in sb["risk_flags"]))
check("含待确认事项", all(c[:6] in full for c in sb["pending_confirmations"][:2]))
check("中文标题正常", "短视频拍摄执行单" in full)
check("总时长行存在", "总拍摄时长" in full)

print("\n==== 汇总 ====")
failed = [n for n, ok, _ in results if not ok]
print(f"共 {len(results)} 项，通过 {len(results)-len(failed)} 项")
sys.exit(1 if failed else 0)
