"""022：导出路由（从 app.py 拆分）。

- POST /api/export              分镜 DOCX 导出（拍摄执行单）
- POST /api/shooting-checklist  现场拍摄清单 DOCX
- POST /api/validate            分镜校验（时长/镜头数）
- POST /api/export-srt           SRT 字幕导出

017：临时文件下载后清理，避免磁盘增长。
"""
from __future__ import annotations

import os
import tempfile
from typing import List

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from ...exporter import export_checklist_docx, export_docx, storyboard_to_srt
from ...validator import validate_dict

router = APIRouter(tags=["exports"])


class ExportRequest(BaseModel):
    storyboard: dict


class ChecklistRequest(BaseModel):
    storyboard: dict
    shoot_state: dict = Field(default_factory=dict)


def _cleanup_temp_file(path: str):
    """017: 导出文件下载完成后清理临时文件，避免磁盘持续增长。"""
    try:
        os.remove(path)
    except OSError:
        pass


@router.post("/api/export")
def export_plan(req: ExportRequest):
    errors, sb = validate_dict(req.storyboard)
    if errors:
        raise HTTPException(status_code=400,
                            detail="分镜未通过校验，无法导出：" + "; ".join(errors[:3]))
    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    tmp.close()
    export_docx(sb.model_dump(), tmp.name)
    return FileResponse(tmp.name, filename="拍摄执行单.docx",
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        background=BackgroundTask(_cleanup_temp_file, tmp.name))


@router.post("/api/shooting-checklist")
def export_checklist(req: ChecklistRequest):
    """现场拍摄清单导出（V3）：轻量清单，不替换完整分镜导出。"""
    errors, sb = validate_dict(req.storyboard)
    if errors:
        raise HTTPException(status_code=400,
                            detail="分镜未通过校验，无法导出：" + "; ".join(errors[:3]))
    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    tmp.close()
    export_checklist_docx(sb.model_dump(), req.shoot_state, tmp.name)
    return FileResponse(tmp.name, filename="现场拍摄清单.docx",
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        background=BackgroundTask(_cleanup_temp_file, tmp.name))


@router.post("/api/validate")
def validate_sb(req: ExportRequest):
    errors, sb = validate_dict(req.storyboard)
    total = sum(s.duration_seconds for s in sb.shots) if sb else 0
    return {"valid": not errors, "errors": errors, "total_seconds": total,
            "shot_count": len(sb.shots) if sb else 0}


@router.post("/api/export-srt")
def export_srt(req: ExportRequest):
    errors, sb = validate_dict(req.storyboard)
    if errors:
        raise HTTPException(status_code=400, detail="分镜未通过校验，无法导出 SRT：" + "; ".join(errors[:3]))
    try:
        content = storyboard_to_srt(sb.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    tmp = tempfile.NamedTemporaryFile(suffix=".srt", delete=False)
    tmp.write(content.encode("utf-8-sig"))
    tmp.close()
    return FileResponse(tmp.name, filename="字幕.srt", media_type="application/x-subrip",
                        background=BackgroundTask(_cleanup_temp_file, tmp.name))
