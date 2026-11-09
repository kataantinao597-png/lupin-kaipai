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
from ...validator import _normalize_storyboard_for_validation, validate_dict

router = APIRouter(tags=["exports"])


class ExportRequest(BaseModel):
    storyboard: dict


class ChecklistRequest(BaseModel):
    storyboard: dict
    shoot_state: dict = Field(default_factory=dict)


def _err_detail(prefix, errors):
    # 截断只截展示，不掩盖总数；完整列表用 /api/validate 拿（返回 errors 全量）。
    suffix = f"（等共 {len(errors)} 条，完整列表见 /api/validate）" if len(errors) > 3 else ""
    return prefix + "; ".join(errors[:3]) + suffix


def _cleanup_temp_file(path: str):
    """017: 导出文件下载完成后清理临时文件，避免磁盘持续增长。"""
    try:
        os.remove(path)
    except OSError:
        pass


@router.post("/api/export")
def export_plan(req: ExportRequest):
    # 与 Agent 清单路统一：先归一化再校验，简化分镜不再只在导出路报结构错。
    # 导出用归一化后的数据，填充值（中景/固定机位/手机1人）只是占位，用户可在执行单里改。
    errors, sb = validate_dict(_normalize_storyboard_for_validation(req.storyboard))
    if errors:
        raise HTTPException(status_code=400,
                            detail=_err_detail("分镜未通过校验，无法导出：", errors))
    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    tmp.close()
    export_docx(sb.model_dump(), tmp.name)
    return FileResponse(tmp.name, filename="拍摄执行单.docx",
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        background=BackgroundTask(_cleanup_temp_file, tmp.name))


@router.post("/api/shooting-checklist")
def export_checklist(req: ChecklistRequest):
    """现场拍摄清单导出（V3）：轻量清单，不替换完整分镜导出。"""
    errors, sb = validate_dict(_normalize_storyboard_for_validation(req.storyboard))
    if errors:
        raise HTTPException(status_code=400,
                            detail=_err_detail("分镜未通过校验，无法导出：", errors))
    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    tmp.close()
    export_checklist_docx(sb.model_dump(), req.shoot_state, tmp.name)
    return FileResponse(tmp.name, filename="现场拍摄清单.docx",
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        background=BackgroundTask(_cleanup_temp_file, tmp.name))


@router.post("/api/validate")
def validate_sb(req: ExportRequest):
    # 与导出/清单共用同一归一化入口，errors 返回全量，不截断。
    errors, sb = validate_dict(_normalize_storyboard_for_validation(req.storyboard))
    total = sum(s.duration_seconds for s in sb.shots) if sb else 0
    return {"valid": not errors, "errors": errors, "total_seconds": total,
            "shot_count": len(sb.shots) if sb else 0}


@router.post("/api/export-srt")
def export_srt(req: ExportRequest):
    errors, sb = validate_dict(_normalize_storyboard_for_validation(req.storyboard))
    if errors:
        raise HTTPException(status_code=400, detail=_err_detail("分镜未通过校验，无法导出 SRT：", errors))
    try:
        content = storyboard_to_srt(sb.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    tmp = tempfile.NamedTemporaryFile(suffix=".srt", delete=False)
    tmp.write(content.encode("utf-8-sig"))
    tmp.close()
    return FileResponse(tmp.name, filename="字幕.srt", media_type="application/x-subrip",
                        background=BackgroundTask(_cleanup_temp_file, tmp.name))
