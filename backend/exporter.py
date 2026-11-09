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

# 风险"无异常"文案统一：以 backend/risk.py 的常量为准，
# 措辞守住"不把未发现风险表述为已保证合规"这条线。
try:
    from .risk import NO_RISK_FOUND_TEXT
except Exception:  # 独立脚本运行时回退，避免循环导入拖垮导出
    NO_RISK_FOUND_TEXT = (
        "本脚本旁白/字幕中未发现需要标注的风险断言。"
        "但以下待确认事项发布前必须人工确认，本工具不保证广告合规。"
    )

VIDEO_PURPOSE_LABELS = {
    "brand": "品牌宣传",
    "sales": "商品销售",
    "intro": "产品介绍",
    "record": "产地记录",
}


def _g(d, key, default=""):
    """dict 安全取值（v1 老数据缺新字段时不报错）。"""
    if not isinstance(d, dict):
        return default
    v = d.get(key, default)
    return default if v is None else v


#: S-04：validator 归一化填的占位文案（validator._normalize_...），
#: 不是真实产品信息；导出件里必须标出来，不能当产品信息印。
_PLACEHOLDER_PRODUCT_SUMMARY = "AI 助手生成"


def _product_summary_text(data: dict) -> str:
    """导出用产品概述：占位值标“（待确认）”，真实值原样印。"""
    v = str(_g(data, "product_summary"))
    if v == _PLACEHOLDER_PRODUCT_SUMMARY:
        return v + "（待确认）"
    return v


def _shot_final_seconds(s: dict) -> float:
    """成片使用时长：优先 final_duration_seconds，缺省回退 duration_seconds（v1 兼容），
    再回退 duration_sec（Agent 工具链简化格式）。"""
    v = _g(s, "final_duration_seconds", None)
    if v is None:
        v = _g(s, "duration_seconds", None)
    if v is None:
        v = _g(s, "duration_sec", 0)
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _shot_record_seconds(s: dict) -> float:
    """建议录制时长：优先 record_duration_seconds，缺省回退 duration_seconds（v1 兼容），
    再回退 duration_sec（Agent 工具链简化格式）。"""
    v = _g(s, "record_duration_seconds", None)
    if v is None:
        v = _g(s, "duration_seconds", None)
    if v is None:
        v = _g(s, "duration_sec", 0)
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _fmt_seconds(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f == int(f) else str(round(f, 1))


def _estimate_shoot_minutes(shots) -> int:
    """预计现场拍摄耗时（分钟）：sum(建议录制时长) * 1.5（含转场/走位），向上取整，至少 1 分钟。"""
    total = sum(_shot_record_seconds(s) for s in shots)
    import math
    return max(1, math.ceil(total * 1.5 / 60))


def _label_para(doc, label: str, text: str, keep=False):
    """'标签：内容' 段落，标签加粗。空内容直接跳过（v1 兼容）。"""
    text = (text or "").strip()
    if not text:
        return None
    p = doc.add_paragraph()
    r = p.add_run(label + "：")
    r.bold = True
    p.add_run(text)
    if keep:
        p.paragraph_format.keep_together = True
    fix_font(p)
    return p


def _shot_duration_text(s: dict) -> str:
    """时长行：v2 显示'录制Xs / 成片Ys'，v1 只显示'Xs'。"""
    rec = _g(s, "record_duration_seconds", None)
    final = _shot_final_seconds(s)
    if rec is None:
        return _fmt_seconds(final) + "s"
    return "录制%s / 成片%ss" % (_fmt_seconds(rec), _fmt_seconds(final))


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
    """拍摄执行单 DOCX（V2.0）。

    章节：一、任务概要 / 二、分镜表（每镜头独立小节） / 三、素材准备清单 /
    四、宣传事实风险提示 / 五、用户待确认事项。
    v1 老数据（无 v2 新字段）可正常导出，缺字段的节自动跳过。
    """
    doc = Document()
    doc.styles["Normal"].font.size = Pt(10.5)

    title = doc.add_heading("短视频拍摄执行单", level=0)
    title.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
    fix_font(title)
    para(doc, "（鲁品开拍 · AI 辅助生成，须经人工确认后执行）")

    # ---------- 一、任务概要 ----------
    heading(doc, "一、任务概要", level=1)
    para(doc, "产品：" + _product_summary_text(data))
    para(doc, "目标受众：" + str(_g(data, "target_audience")))
    para(doc, "创意方向：" + str(_g(data, "creative_direction")))
    purpose = _g(data, "video_purpose", "")
    para(doc, "视频用途：" + VIDEO_PURPOSE_LABELS.get(purpose, purpose or "未指定"))
    shots = data.get("shots") or []
    final_total = sum(_shot_final_seconds(s) for s in shots)
    para(doc, "成片目标时长：%s 秒（目标 %s 秒）" % (
        _fmt_seconds(final_total), _fmt_seconds(_g(data, "target_duration_seconds", 0))), bold=True)
    para(doc, "预计现场拍摄耗时：约 %d 分钟（含机位架设、走位与多条录制）" % _estimate_shoot_minutes(shots))
    shoot = data.get("shoot", {}) or {}
    para(doc, "拍摄条件：%s，%s人%s%s" % (
        shoot.get("device", ""), shoot.get("people", ""),
        ("，" + shoot["lighting"]) if shoot.get("lighting") else "",
        ("，" + shoot["location"]) if shoot.get("location") else "",
    ))

    # ---------- 二、分镜表（每镜头独立小节） ----------
    heading(doc, "二、分镜表", level=1)
    for s in shots:
        # 兼容 Agent 工具链简化格式（shot_no）
        num = _g(s, "shot_number", None) or _g(s, "shot_no", "?")
        heading(doc, "镜头 %s" % num, level=2)

        _label_para(doc, "镜头目的", _g(s, "shot_purpose"))
        _label_para(doc, "时长", _shot_duration_text(s))
        _label_para(doc, "景别/运镜",
                    ("%s / %s" % (_g(s, "shot_type"), _g(s, "camera_movement"))).strip(" /"))
        _label_para(doc, "手机摆放与光线", _g(s, "phone_setup"))
        _label_para(doc, "前置条件", "；".join(_g(s, "prerequisites") or []) if isinstance(_g(s, "prerequisites"), list) else _g(s, "prerequisites"))
        _label_para(doc, "画面", _g(s, "scene_description"))
        _label_para(doc, "拍摄动作", _g(s, "shooting_action"))

        steps = _g(s, "shoot_steps", []) or []
        if steps:
            p = doc.add_paragraph()
            r = p.add_run("拍摄步骤：")
            r.bold = True
            fix_font(p)
            for i, st in enumerate(steps, 1):
                ip = doc.add_paragraph("%d. %s" % (i, st))
                ip.paragraph_format.left_indent = Cm(0.75)
                fix_font(ip)

        criteria = _g(s, "acceptance_criteria", []) or []
        if criteria:
            p = doc.add_paragraph()
            r = p.add_run("验收标准（逐条对照，全部打勾再过）：")
            r.bold = True
            fix_font(p)
            for c in criteria:
                ip = doc.add_paragraph("☐ " + str(c))
                ip.paragraph_format.left_indent = Cm(0.75)
                fix_font(ip)

        fmodes = _g(s, "failure_modes", []) or []
        if fmodes:
            p = doc.add_paragraph()
            r = p.add_run("常见失败与处理：")
            r.bold = True
            fix_font(p)
            for fm in fmodes:
                if isinstance(fm, dict):
                    sym, fix = fm.get("symptom", ""), fm.get("fix", "")
                else:
                    sym, fix = str(fm), ""
                line = ("⚠ %s → %s" % (sym, fix)) if fix else ("⚠ " + sym)
                ip = doc.add_paragraph(line)
                ip.paragraph_format.left_indent = Cm(0.75)
                fix_font(ip)

        _label_para(doc, "补拍指引", _g(s, "reshoot_guide"))
        _label_para(doc, "替代拍法", _g(s, "alt_approach"))
        trans = _g(s, "transition")
        enote = _g(s, "edit_notes")
        if trans or enote:
            combo = "；".join(t for t in [trans and ("转场：" + trans),
                                         enote and ("剪辑：" + enote)] if t)
            _label_para(doc, "转场与剪辑", combo)

        vo, sub = _g(s, "voiceover"), _g(s, "subtitle")
        if vo or sub:
            p = doc.add_paragraph()
            r = p.add_run("旁白 / 字幕：")
            r.bold = True
            fix_font(p)
            if vo:
                vp = doc.add_paragraph("旁白：" + vo)
                vp.paragraph_format.left_indent = Cm(0.75)
                fix_font(vp)
            if sub:
                sp = doc.add_paragraph("字幕：" + sub)
                sp.paragraph_format.left_indent = Cm(0.75)
                fix_font(sp)

        props = _g(s, "props", []) or []
        assets = _g(s, "required_assets", []) or []
        if props or assets:
            parts = []
            if props:
                parts.append("道具：" + "、".join(props))
            if assets:
                parts.append("素材：" + "、".join(assets))
            _label_para(doc, "道具与素材", "；".join(parts))

        fact_refs = _g(s, "fact_refs", []) or []
        if fact_refs:
            _label_para(doc, "引用事实", "、".join(str(f) for f in fact_refs))

    # ---------- 三、素材准备清单 ----------
    heading(doc, "三、素材准备清单", level=1)
    for m in data.get("materials_checklist") or []:
        p = doc.add_paragraph(str(m), style="List Bullet")
        p.paragraph_format.keep_together = True
        fix_font(p)

    # ---------- 四、宣传事实风险提示 ----------
    heading(doc, "四、宣传事实风险提示", level=1)
    para(doc, "说明：以下内容缺乏充分依据，请人工核实确认后再决定是否使用。本工具不保证广告合规。")
    risk_flags = data.get("risk_flags") or []
    if risk_flags:
        for r in risk_flags:
            status = "【已人工确认】" if r.get("confirmed") else "【待人工确认】"
            src = r.get("source", "")
            shot_ref = r.get("shot_number") or r.get("shot_ref") or ""
            where = ("（关联：镜头 %s" % shot_ref) if shot_ref else "(关联："
            text = r.get("text", "")
            if where.endswith("（关联：") and text:
                where += "文案「%s」" % (text[:20] + ("…" if len(text) > 20 else ""))
            where += ")"
            para(doc, "%s【%s风险】%s\n原因：%s%s%s" % (
                status, r.get("level", ""), text, r.get("reason", ""),
                where, f"（来源：{src}）" if src else ""), keep=True)
    else:
        para(doc, NO_RISK_FOUND_TEXT)

    # ---------- 五、用户待确认事项 ----------
    heading(doc, "五、用户待确认事项", level=1)
    pending = data.get("pending_confirmations") or []
    if pending:
        for i, c in enumerate(pending, 1):
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
    para(doc, f"作品：{_product_summary_text(data)}")
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
        # 兼容 Agent 工具链简化格式（duration_sec）；S-05：key 存在但
        # 值为 None 时 float(None) 会报 TypeError，先 or 掉 None。
        duration = float(shot.get("duration_seconds")
                         or shot.get("duration_sec") or 0)
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

