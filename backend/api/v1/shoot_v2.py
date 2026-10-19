"""V2.0 P1：实拍验收 + 补拍清单 + 剪辑交付清单。

路由前缀：/api/v2/shoot（完整路径，不依赖 _mount 的 prefix 参数）。

- PATCH /api/v2/shoot/works/{work_id}/shots/accept
  镜头验收状态写入（accepted/needs_reshoot/pending），与 v5 拍摄状态独立。
  验收数据存 works.data_json 顶层 key `shot_acceptance`：
  {shot_key: {status, note, asset_ids[], shot_number, updated_at}}，
  shot_key 格式由前端约定（如 s3_xxxx），后端透传不强制校验格式。
- GET /api/v2/shoot/works/{work_id}/reshoot-list
  补拍清单：只列 status==needs_reshoot 的镜头。无补拍返回空列表，
  不得伪造"全部通过"。
- GET /api/v2/shoot/works/{work_id}/edit-list
  剪辑交付清单 JSON：时长四分法（建议录制/原始素材/可用/成片），
  缺数据标 null 不编造；未验收一律 pending（需要人工确认）。
- GET /api/v2/shoot/works/{work_id}/edit-list.docx
  上面清单的 DOCX 下载。

铁律：
- 没有实际运行的检查绝不标"通过"；
- AI 无法判断的素材状态一律 pending；
- 不得生成质检分数。
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from typing import Any

from docx import Document
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ... import models
from ...auth.database import get_db
from ...auth.deps import get_current_user
from ...auth.models import User
from ...core import error_codes
from ...core.exceptions import BusinessError
from ...exporter import fix_font, heading, para, set_no_row_split, set_repeat_header
from ...rbac import require_work_access

router = APIRouter(tags=["shoot-v2"])

ACCEPTANCE_KEY = "shot_acceptance"

ACCEPT_STATUSES = ("accepted", "needs_reshoot", "pending")

PRE_PUBLISH_CHECKLIST = [
    "旁白与画面是否对应",
    "字幕错别字检查",
    "待确认事项已人工确认",
    "风险提示已通读",
]


class AcceptShotRequest(BaseModel):
    """镜头验收提交。"""

    shot_key: str = Field(..., description="前端约定的镜头键，如 s3_a1b2c3")
    shot_number: int = Field(default=0, description="镜头编号，用于回查分镜信息")
    status: str = Field(..., description="accepted|needs_reshoot|pending")
    note: str = Field(default="", description="验收备注/问题说明")
    asset_ids: list[str] = Field(default_factory=list, description="关联的素材 id")


# ---------- 内部工具 ----------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_data(work: models.Work) -> dict:
    try:
        data = json.loads(work.data_json or "{}")
    except (ValueError, TypeError):
        data = {}
    return data if isinstance(data, dict) else {}


def _save_data(db: Session, work: models.Work, data: dict) -> None:
    work.data_json = json.dumps(data, ensure_ascii=False)
    db.commit()


def _shots(data: dict) -> list[dict]:
    sb = data.get("storyboard") or {}
    shots = sb.get("shots") or []
    return [s for s in shots if isinstance(s, dict)]


def _acceptance(data: dict) -> dict:
    acc = data.get(ACCEPTANCE_KEY) or {}
    return acc if isinstance(acc, dict) else {}


def _g(shot: dict, key: str, default: Any = None) -> Any:
    v = shot.get(key, default)
    return v if v is not None else default


def _final_seconds(shot: dict) -> Any:
    """成片使用时长：优先 final_duration_seconds，回退 duration_seconds。"""
    v = _g(shot, "final_duration_seconds")
    if v is None:
        v = _g(shot, "duration_seconds")
    return v


def _acceptance_for_shot(acceptance: dict, shot_number: int) -> dict | None:
    """按 shot_number 回查验收记录（同一镜头多个 key 取最近更新）。"""
    cands = []
    for key, rec in acceptance.items():
        if not isinstance(rec, dict):
            continue
        if rec.get("shot_number") == shot_number:
            cands.append((rec.get("updated_at") or "", rec))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0], reverse=True)
    return cands[0][1]


def _shot_map(shots: list[dict]) -> dict[int, dict]:
    return {s.get("shot_number"): s for s in shots
            if isinstance(s.get("shot_number"), int)}


def _asset_durations(db: Session, org_id: str, asset_ids: list[str]) -> float | None:
    """关联素材的原始时长总和（秒）。

    从 assets.meta_json 取 duration；任一素材无时长数据则该素材不计入；
    全部无数据返回 None（不编造）。
    """
    if not asset_ids:
        return None
    total = 0.0
    any_data = False
    assets = (db.query(models.Asset)
              .filter(models.Asset.id.in_(asset_ids),
                      models.Asset.org_id == org_id)
              .all())
    for a in assets:
        try:
            meta = json.loads(a.meta_json or "{}")
        except (ValueError, TypeError):
            meta = {}
        dur = meta.get("duration") if isinstance(meta, dict) else None
        if isinstance(dur, (int, float)) and dur > 0:
            total += float(dur)
            any_data = True
    return total if any_data else None


def _shot_key_for(shot_number: int) -> str:
    return f"s{shot_number}"


# ---------- 1. 验收写入 ----------

@router.patch("/api/v2/shoot/works/{work_id}/shots/accept")
def accept_shot(
    work_id: str,
    req: AcceptShotRequest,
    access: tuple = Depends(require_work_access("work:update")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """写入单个镜头的验收状态。

    权限：work:update。asset_ids 必须存在且属于同一组织，否则 400。
    """
    _work, _ms = access
    # Depends 会话可能已关闭，当前会话重新加载以获得可写对象（同 works.py）
    work = (db.query(models.Work)
            .filter(models.Work.id == work_id,
                    models.Work.deleted_at.is_(None))
            .first())
    if work is None:
        raise BusinessError("作品不存在",
                            code=error_codes.WORK_NOT_FOUND, http_status=404)
    if req.status not in ACCEPT_STATUSES:
        raise BusinessError(
            f"非法验收状态: {req.status}（仅允许 {','.join(ACCEPT_STATUSES)}）",
            code=error_codes.INVALID_PARAM, http_status=400)
    if not req.shot_key or not req.shot_key.strip():
        raise BusinessError("shot_key 不能为空",
                            code=error_codes.INVALID_PARAM, http_status=400)
    # 校验 asset_ids：存在且同组织
    asset_ids = [a for a in (req.asset_ids or []) if a]
    if asset_ids:
        found = (db.query(models.Asset.id)
                 .filter(models.Asset.id.in_(asset_ids),
                         models.Asset.org_id == work.org_id)
                 .all())
        found_ids = {row[0] for row in found}
        bad = [a for a in asset_ids if a not in found_ids]
        if bad:
            raise BusinessError(
                f"素材不存在或不属于本组织: {','.join(bad[:5])}",
                code=error_codes.INVALID_PARAM, http_status=400)

    data = _load_data(work)
    acceptance = _acceptance(data)
    record = {
        "status": req.status,
        "note": req.note or "",
        "asset_ids": asset_ids,
        "shot_number": req.shot_number,
        "updated_at": _now_iso(),
        "updated_by": user.id,
    }
    acceptance[req.shot_key.strip()] = record
    data[ACCEPTANCE_KEY] = acceptance
    _save_data(db, work, data)
    return {"shot_key": req.shot_key.strip(), "acceptance": record}


# ---------- 2. 补拍清单 ----------

@router.get("/api/v2/shoot/works/{work_id}/reshoot-list")
def reshoot_list(
    work_id: str,
    access: tuple = Depends(require_work_access("work:read")),
    db: Session = Depends(get_db),
):
    """补拍清单：只返回验收状态为 needs_reshoot 的镜头。

    无补拍时返回空列表——空就是空，不伪造"全部通过"。
    """
    work, _ms = access
    data = _load_data(work)
    shots = _shots(data)
    acceptance = _acceptance(data)
    shot_map = _shot_map(shots)

    out = []
    for key, rec in acceptance.items():
        if not isinstance(rec, dict) or rec.get("status") != "needs_reshoot":
            continue
        shot_number = rec.get("shot_number")
        shot = shot_map.get(shot_number) if isinstance(shot_number, int) else None
        shot = shot or {}
        out.append({
            "shot_key": key,
            "shot_number": shot_number,
            "shot_purpose": _g(shot, "shot_purpose"),
            "scene_description": _g(shot, "scene_description", ""),
            "note": rec.get("note") or "",
            "reshoot_guide": _g(shot, "reshoot_guide"),
            "failure_modes": _g(shot, "failure_modes") or [],
            "alt_approach": _g(shot, "alt_approach"),
        })
    out.sort(key=lambda x: (x["shot_number"] if isinstance(x["shot_number"], int)
                            else 9999))
    return {"items": out}


# ---------- 3. 剪辑交付清单 ----------

def _build_edit_list(db: Session, work: models.Work) -> dict:
    """组装剪辑交付清单（JSON 与 DOCX 共用）。

    时长四分法（秒）：
    - record_seconds：建议录制时长（分镜计划）
    - raw_asset_seconds：关联素材原始时长总和（无数据为 null，不编造）
    - usable_seconds：预计可用素材时长（分镜计划）
    - final_seconds：成片使用时长
    """
    data = _load_data(work)
    shots = _shots(data)
    acceptance = _acceptance(data)

    items = []
    for s in sorted(shots, key=lambda x: x.get("shot_number", 9999)):
        sn = s.get("shot_number")
        rec = _acceptance_for_shot(acceptance, sn) if isinstance(sn, int) else None
        status = (rec or {}).get("status") or "pending"
        asset_ids = (rec or {}).get("asset_ids") or []
        items.append({
            "shot_number": sn,
            "shot_purpose": _g(s, "shot_purpose"),
            "scene_description": _g(s, "scene_description", ""),
            "record_seconds": _g(s, "record_duration_seconds"),
            "raw_asset_seconds": _asset_durations(db, work.org_id, asset_ids),
            "usable_seconds": _g(s, "usable_duration_seconds"),
            "final_seconds": _final_seconds(s),
            "voiceover": _g(s, "voiceover", ""),
            "subtitle": _g(s, "subtitle", ""),
            "transition": _g(s, "transition"),
            "edit_notes": _g(s, "edit_notes"),
            "acceptance_status": status,
            "asset_ids": asset_ids,
            "missing": status != "accepted",
        })

    total_final = sum(i["final_seconds"] for i in items
                      if isinstance(i["final_seconds"], (int, float)))
    recs = [i["record_seconds"] for i in items
            if isinstance(i["record_seconds"], (int, float))]
    total_record = sum(recs) if recs else None
    accepted_count = sum(1 for i in items if i["acceptance_status"] == "accepted")
    missing_shots = [i["shot_number"] for i in items if i["missing"]]

    product = (data.get("product") or {})
    return {
        "work_id": work.id,
        "title": work.title,
        "product_name": product.get("product_name") or product.get("name") or "",
        "items": items,
        "summary": {
            "total_final_seconds": total_final,
            "total_record_seconds": total_record,
            "accepted_count": accepted_count,
            "total_count": len(items),
            "missing_shots": missing_shots,
            "pre_publish_checklist": list(PRE_PUBLISH_CHECKLIST),
        },
    }


@router.get("/api/v2/shoot/works/{work_id}/edit-list")
def edit_list(
    work_id: str,
    access: tuple = Depends(require_work_access("work:read")),
    db: Session = Depends(get_db),
):
    """剪辑交付清单 JSON。v1 老作品（无 acceptance）不报错，未验收为 pending。"""
    work, _ms = access
    return _build_edit_list(db, work)


# ---------- 4. 剪辑交付清单 DOCX ----------

def _fmt_sec(v: Any) -> str:
    if isinstance(v, (int, float)):
        return f"{v:g}s"
    return "—"


def build_edit_list_doc(payload: dict) -> Document:
    """剪辑交付清单 DOCX（章节：时间轴表 / 缺失镜头处理 / 转场音乐建议 / 发布前检查）。"""
    doc = Document()
    title = payload.get("title") or "剪辑交付清单"
    h = doc.add_heading(f"剪辑交付清单：{title}", level=1)
    fix_font(h)
    if payload.get("product_name"):
        para(doc, f"产品：{payload['product_name']}")
    para(doc, "AI 辅助生成，仅供剪辑参考，须经人工确认后执行。")

    items = payload.get("items") or []
    summary = payload.get("summary") or {}

    # 一、时间轴表
    heading(doc, "一、成片时间轴", level=2)
    table = doc.add_table(rows=1, cols=5)
    table.style = "Table Grid"
    hdr = table.rows[0].cells
    for i, t in enumerate(["镜头", "成片时长", "旁白", "字幕", "素材状态"]):
        hdr[i].text = t
        fix_font(hdr[i].paragraphs[0])
    set_repeat_header(table.rows[0])
    status_text = {"accepted": "验收通过", "needs_reshoot": "需补拍",
                   "pending": "待验收（需人工确认）"}
    for it in items:
        row = table.add_row().cells
        row[0].text = f"镜{it['shot_number']}"
        row[1].text = _fmt_sec(it["final_seconds"])
        row[2].text = (it["voiceover"] or "")[:80]
        row[3].text = (it["subtitle"] or "")[:80]
        row[4].text = status_text.get(it["acceptance_status"],
                                     "待验收（需人工确认）")
        for c in row:
            fix_font(c.paragraphs[0])
        set_no_row_split(table.rows[-1])
    tf = summary.get("total_final_seconds")
    para(doc, f"成片总时长：{_fmt_sec(tf)}（各镜头成片时长之和）")

    # 二、缺失镜头处理
    heading(doc, "二、缺失镜头处理", level=2)
    missing = [it for it in items if it["missing"]]
    if not missing:
        para(doc, "当前无缺失镜头（全部验收通过）。如验收状态有误，请在执行页重新确认。")
    else:
        for it in missing:
            para(doc, f"镜{it['shot_number']}（{it['acceptance_status']}）："
                      f"{it['shot_purpose'] or it['scene_description'] or ''}",
                 bold=True)
            guide = None
            # 从原始分镜取 reshoot_guide（payload 不含则留空，不编造）
            para(doc, "处理建议：按分镜中的补拍指引重拍该镜头；"
                      "如现场条件不允许，用替代拍法补齐同等时长素材。")

    # 三、转场音乐建议
    heading(doc, "三、转场与音乐建议", level=2)
    has_detail = False
    for it in items:
        bits = []
        if it["transition"]:
            bits.append(f"转场：{it['transition']}")
        if it["edit_notes"]:
            bits.append(f"剪辑备注：{it['edit_notes']}")
        if bits:
            has_detail = True
            para(doc, f"镜{it['shot_number']}：" + "；".join(bits))
    para(doc, "通用建议：背景音乐选节奏轻快、无歌词的纯音乐，音量低于旁白；"
              "环境声（如果园鸟鸣、厨房烟火气）可低音量垫底增强真实感。")

    # 四、发布前检查
    heading(doc, "四、发布前检查", level=2)
    for c in summary.get("pre_publish_checklist") or []:
        para(doc, f"☐ {c}")

    return doc


@router.get("/api/v2/shoot/works/{work_id}/edit-list.docx")
def edit_list_docx(
    work_id: str,
    access: tuple = Depends(require_work_access("work:read")),
    db: Session = Depends(get_db),
):
    """剪辑交付清单 DOCX 下载。"""
    work, _ms = access
    payload = _build_edit_list(db, work)
    doc = build_edit_list_doc(payload)
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    from urllib.parse import quote
    fname = quote(f"剪辑交付清单_{work.title or work.id}.docx")
    return StreamingResponse(
        buf,
        media_type=("application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"),
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{fname}"},
    )
