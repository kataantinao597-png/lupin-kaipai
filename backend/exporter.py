"""鲁品开拍 - 拍摄执行单 DOCX 导出（优化版）。

优化点（相对实验版）：
1. 分镜表跨页时重复表头（w:tblHeader）
2. 表格行不跨页断开（w:cantSplit）
3. 素材清单段落尽量同页（keep_together）
4. 风险提示标注来源与人工确认状态；待确认事项完整
"""
import json
from docx import Document
from docx.shared import Pt, RGBColor, Cm
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
from docx.oxml import parse_xml
from docx.oxml.ns import qn, nsdecls

FONT = "微软雅黑"


def fix_font(paragraph):
    for r in paragraph.runs:
        r.font.name = FONT
        r._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)


def para(doc, text, bold=False, keep=False):
    p = doc.add_paragraph()
    r = p.add_run(text)
    r.bold = bold
    if keep:
        p.paragraph_format.keep_together = True
    fix_font(p)
    return p


def heading(doc, text, level):
    h = doc.add_heading(text, level=level)
    for run in h.runs:
        run.font.color.rgb = RGBColor(0x1F, 0x49, 0x7D)
    fix_font(h)
    return h


def set_repeat_header(row):
    """表头行跨页重复。"""
    for cell in row.cells:
        tcPr = cell._tc.get_or_add_tcPr()
        tcPr.append(parse_xml(f'<w:tblHeader {nsdecls("w")} w:val="true"/>'))


def set_no_row_split(row):
    """行内不跨页断开。"""
    trPr = row._tr.get_or_add_trPr()
    trPr.append(parse_xml(f'<w:cantSplit {nsdecls("w")} w:val="true"/>'))


def build_doc(data: dict) -> Document:
    doc = Document()
    doc.styles["Normal"].font.size = Pt(10.5)

    title = doc.add_heading("短视频拍摄执行单", level=0)
    title.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
    fix_font(title)
    para(doc, "（鲁品开拍 · AI 辅助生成，须经人工确认后执行）")

    heading(doc, "一、产品信息", level=1)
    para(doc, "产品：" + data["product_summary"])
    para(doc, "目标受众：" + data["target_audience"])
    para(doc, "创意方向：" + data["creative_direction"])
    para(doc, "目标时长：" + str(data["target_duration_seconds"]) + " 秒")
    shoot = data.get("shoot", {})
    para(doc, "拍摄条件：%s，%s人%s%s" % (
        shoot.get("device", ""), shoot.get("people", ""),
        ("，" + shoot["lighting"]) if shoot.get("lighting") else "",
        ("，" + shoot["location"]) if shoot.get("location") else "",
    ))

    heading(doc, "二、分镜表", level=1)
    shots = data["shots"]
    table = doc.add_table(rows=1, cols=6)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    hdr = ["镜头", "时长", "景别/运镜", "画面与拍摄动作", "旁白/字幕", "道具与素材"]
    for i, htxt in enumerate(hdr):
        cell = table.rows[0].cells[i]
        cell.text = htxt
        for p in cell.paragraphs:
            fix_font(p)
            for r in p.runs:
                r.bold = True
    set_repeat_header(table.rows[0])
    set_no_row_split(table.rows[0])
    for s in shots:
        row = table.add_row().cells
        vals = [
            str(s["shot_number"]),
            str(s["duration_seconds"]) + "s",
            s["shot_type"] + "\n" + s["camera_movement"],
            s["scene_description"] + "\n【动作】" + s["shooting_action"],
            "旁白：" + s["voiceover"] + "\n字幕：" + s["subtitle"],
            "道具：" + "、".join(s["props"]) + "\n素材：" + "、".join(s["required_assets"]),
        ]
        for i, v in enumerate(vals):
            row[i].text = v
            for p in row[i].paragraphs:
                fix_font(p)
        set_no_row_split(table.rows[-1])
    total = sum(s["duration_seconds"] for s in shots)
    para(doc, "总拍摄时长：%s 秒（目标 %s 秒）" % (total, data["target_duration_seconds"]), bold=True)

    heading(doc, "三、素材准备清单", level=1)
    for m in data["materials_checklist"]:
        p = doc.add_paragraph(m, style="List Bullet")
        p.paragraph_format.keep_together = True
        fix_font(p)

    heading(doc, "四、宣传事实风险提示", level=1)
    para(doc, "说明：以下内容缺乏充分依据，请人工核实确认后再决定是否使用。本工具不保证广告合规。")
    if data["risk_flags"]:
        for r in data["risk_flags"]:
            status = "【已人工确认】" if r.get("confirmed") else "【待人工确认】"
            src = r.get("source", "")
            para(doc, "%s【%s风险】%s\n原因：%s%s" % (
                status, r["level"], r["text"], r["reason"],
                f"（来源：{src}）" if src else ""), keep=True)
    else:
        para(doc, "本次未发现明显风险提示（仍建议人工通读旁白/字幕复核）。")

    heading(doc, "五、用户待确认事项", level=1)
    if data["pending_confirmations"]:
        for i, c in enumerate(data["pending_confirmations"], 1):
            para(doc, "%d. %s" % (i, c), keep=True)
    else:
        para(doc, "无。")

    return doc


def export_docx(data: dict, out_path: str) -> str:
    build_doc(data).save(out_path)
    return out_path


# ---------- 现场拍摄清单导出（V3） ----------

def build_checklist_doc(data: dict, shoot_state: dict) -> Document:
    """拍摄清单：轻量、可打印、带勾选框和补拍备注。"""
    doc = Document()
    doc.styles["Normal"].font.size = Pt(10.5)

    shots = data.get("shots", [])
    status_map = (shoot_state or {}).get("shots", {})
    STATUS_LABEL = {"todo": "待拍摄", "done": "已拍摄", "reshoot": "需要补拍"}

    heading(doc, "现场拍摄清单", level=1)
    para(doc, f"作品：{data.get('product_summary', '')}")
    para(doc, f"目标时长：{data.get('target_duration_seconds', '')}秒　镜头总数：{len(shots)}")

    done = sum(1 for s in shots if status_map.get(str(s.get("shot_number")), {}).get("status") == "done")
    para(doc, f"拍摄进度：{done}/{len(shots)} 已拍摄", bold=True)

    heading(doc, "镜头清单", level=2)
    for s in shots:
        num = s.get("shot_number")
        st = status_map.get(str(num), {})
        label = STATUS_LABEL.get(st.get("status", "todo"), "待拍摄")
        box = "☑" if st.get("status") == "done" else "☐"
        para(doc, f"{box} 镜头 {num}（{s.get('duration_seconds', '')}s）[{label}]", bold=True)
        if s.get("scene_description"):
            para(doc, f"　画面：{s['scene_description']}")
        if s.get("shooting_action"):
            para(doc, f"　动作：{s['shooting_action']}")
        if s.get("camera_movement"):
            para(doc, f"　机位：{s['camera_movement']}")
        props = (s.get("props") or []) + (s.get("required_assets") or [])
        if props:
            para(doc, f"　道具：{', '.join(props)}")
        if s.get("voiceover"):
            para(doc, f"　口播：{s['voiceover']}")
        if st.get("status") == "reshoot" and st.get("note"):
            para(doc, f"　⚠ 补拍备注：{st['note']}")

    reshoots = [(s, status_map[str(s.get("shot_number"))])
                for s in shots
                if status_map.get(str(s.get("shot_number")), {}).get("status") == "reshoot"]
    if reshoots:
        heading(doc, "补拍清单", level=2)
        for s, st in reshoots:
            para(doc, f"☐ 镜头 {s.get('shot_number')}：{st.get('note') or '（未填写原因）'}")

    return doc


def export_checklist_docx(data: dict, shoot_state: dict, out_path: str) -> str:
    build_checklist_doc(data, shoot_state).save(out_path)
    return out_path


if __name__ == "__main__":
    import sys
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    export_docx(data, sys.argv[2])
    print("DOCX 已生成: " + sys.argv[2])
def _srt_timestamp(seconds: float) -> str:
    if seconds < 0:
        raise ValueError("SRT timestamp cannot be negative")
    millis = int(round(seconds * 1000))
    hours, remainder = divmod(millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

def storyboard_to_srt(storyboard: dict) -> str:
    """Convert storyboard subtitles to UTF-8 SRT text using cumulative timing."""
    entries = []
    cursor = 0.0
    for index, shot in enumerate(storyboard.get("shots", []), start=1):
        duration = float(shot.get("duration_seconds", 0))
        subtitle = str(shot.get("subtitle", "")).strip()
        if duration <= 0:
            raise ValueError(f"shot {index} duration must be positive")
        if not subtitle:
            raise ValueError(f"shot {index} has no subtitle")
        end = cursor + duration
        entries.append(f"{index}\n{_srt_timestamp(cursor)} --> {_srt_timestamp(end)}\n{subtitle}\n")
        cursor = end
    if not entries:
        raise ValueError("storyboard is empty")
    return "\n".join(entries)

