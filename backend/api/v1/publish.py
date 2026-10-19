"""V3.0 F：发布中心 API。

为每次发布建立独立记录，关联源拍摄项目 / 文案版本 / 平台版本 /
最终视频与封面文件。

铁律：
- 首期只支持复制、下载、手动记录发布结果；不做未经验证的第三方平台
  发布接口，界面与文案中绝不出现"自动发布"字样。
- 状态机由后端 PUBLISH_TRANSITIONS 强制；生成文案、完成剪辑、实际发布
  是不同阶段，绝不因前一阶段成功自动推进后续状态。
- 发布前检查由后端 run_prepublish_check 执行；review → ready 要求所有
  blocking 项通过；有未处理的重大风险（RiskFlag open 且 level 高/中）
  时明确阻止，不显示为审核通过。
- 只记录引用关系，不复制文件；删除引用 ≠ 删除实体文件。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models
from ...auth.database import get_db
from ...auth.deps import get_current_user
from ...auth.models import User
from ...core import error_codes
from ...core.exceptions import BusinessError
from ...core.response import success
from ...rbac import assert_can, get_membership, has_permission
from ...repositories.publish_repository import (
    PublishRecordNotFoundError,
    PublishRepository,
)

router = APIRouter(prefix="/publish", tags=["v1-publish"])

# 重大风险：RiskFlag status=open 且 level 为高/中
RISK_BLOCKING_LEVELS = ("高", "中")
RISK_OPEN = "open"


# ---------- 内部 helper ----------

def _require_publish_perm(db: Session, user: User, org_id: str,
                          action: str, what: str):
    """发布权限；无权一律 404（防枚举，与 copyopt/products 一致）。"""
    if not org_id:
        raise BusinessError("必须指定 org_id",
                            code=error_codes.INVALID_PARAM, http_status=400)
    ms = get_membership(db, user.id, org_id)
    if ms is None or not has_permission(ms.role, action):
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    assert_can(ms.role, action, what)
    return ms


def _rec_or_404(repo: PublishRepository, org_id: str,
                rec_id: str) -> models.PublishRecord:
    try:
        return repo.get(org_id, rec_id)
    except PublishRecordNotFoundError:
        raise BusinessError("发布记录不存在",
                            code=error_codes.PUBLISH_RECORD_NOT_FOUND,
                            http_status=404)


def _item(key: str, label: str, passed: bool, blocking: bool,
          detail: str = "") -> dict:
    return {"key": key, "label": label, "passed": passed,
            "blocking": blocking, "detail": detail or ""}


def _open_blocking_flags(flag_lists: list[list]) -> list[dict]:
    """收集未处理的重大风险（open 且 level 高/中）。"""
    out = []
    for flags in flag_lists:
        for f in flags or []:
            if not isinstance(f, dict):
                continue
            if f.get("status") == RISK_OPEN and \
                    f.get("level") in RISK_BLOCKING_LEVELS:
                out.append({
                    "text": f.get("text") or f.get("reason") or "",
                    "level": f.get("level"),
                    "shot_number": f.get("shot_number"),
                })
    return out


def run_prepublish_check(db: Session, org_id: str,
                         rec: models.PublishRecord) -> list[dict]:
    """发布前检查（后端执行，不能只靠前端）。

    检查项：
    1. 最终视频及必要文件是否就绪（blocking）
    2. 文案是否已人工确认（blocking）
    3. 平台版本标题/正文/封面/话题是否齐全（有关联时 blocking）
    4. 核心产品事实是否有未处理重大风险（blocking）
    5. 授权与相关声明是否已人工确认（blocking）
    6. 源拍摄项目是否存在（非阻断提醒）
    """
    items: list[dict] = []

    # 1. 最终视频
    if rec.video_asset_id:
        asset = db.execute(
            select(models.Asset).where(
                models.Asset.id == rec.video_asset_id,
                models.Asset.org_id == org_id)
        ).scalar_one_or_none()
        if asset is None:
            items.append(_item("video_ready", "最终视频文件", False, True,
                               "关联的视频文件不存在（可能已被删除）"))
        elif asset.status != models.ASSET_STATUS_READY:
            items.append(_item("video_ready", "最终视频文件", False, True,
                               f"视频文件状态为 {asset.status}，尚未就绪"))
        else:
            items.append(_item("video_ready", "最终视频文件", True, True,
                               f"已就绪：{asset.filename or asset.key}"))
    else:
        items.append(_item("video_ready", "最终视频文件", False, True,
                           "未关联最终视频文件"))

    # 2. 文案人工确认
    copy_ver = None
    if rec.copy_version_id:
        copy_ver = db.execute(
            select(models.CopyVersion).where(
                models.CopyVersion.id == rec.copy_version_id,
                models.CopyVersion.org_id == org_id)
        ).scalar_one_or_none()
    if copy_ver is None:
        items.append(_item("copy_confirmed", "文案人工确认", False, True,
                           "未关联文案版本"))
    elif copy_ver.status != models.COPY_STATUS_CONFIRMED:
        items.append(_item("copy_confirmed", "文案人工确认", False, True,
                           f"文案版本 v{copy_ver.version_no} 尚未人工确认 "
                           f"（当前状态：{copy_ver.status}）"))
    else:
        items.append(_item("copy_confirmed", "文案人工确认", True, True,
                           f"已确认：文案版本 v{copy_ver.version_no}"))

    # 3. 平台版本内容齐全
    plat_ver = None
    if rec.platform_version_id:
        plat_ver = db.execute(
            select(models.PlatformVersion).where(
                models.PlatformVersion.id == rec.platform_version_id,
                models.PlatformVersion.org_id == org_id)
        ).scalar_one_or_none()
    if plat_ver is None:
        items.append(_item("platform_content", "平台发布内容", True, False,
                           "未关联平台版本（可直接用文案版本发布）"))
    else:
        missing = []
        if not (plat_ver.title or "").strip():
            missing.append("标题")
        if not (plat_ver.body or "").strip():
            missing.append("正文")
        if not (plat_ver.cover_text or "").strip():
            missing.append("封面文案")
        if not (plat_ver.hashtags or []):
            missing.append("话题标签")
        if missing:
            items.append(_item("platform_content", "平台发布内容", False, True,
                               "平台版本缺少：" + "、".join(missing)))
        else:
            items.append(_item("platform_content", "平台发布内容", True, True,
                               f"齐全：{plat_ver.platform} "
                               f"rev{plat_ver.revision_no}"))

    # 4. 未处理重大风险
    flag_lists = []
    if copy_ver is not None:
        flag_lists.append(copy_ver.risk_flags)
    if plat_ver is not None:
        flag_lists.append(plat_ver.risk_flags)
    blocking_flags = _open_blocking_flags(flag_lists)
    if blocking_flags:
        detail = "；".join(
            f"[{f['level']}] {f['text'][:40]}" for f in blocking_flags[:5])
        if len(blocking_flags) > 5:
            detail += f"…等共 {len(blocking_flags)} 项"
        items.append(_item("risk_open", "重大风险处理", False, True,
                           f"有 {len(blocking_flags)} 项未处理的重大风险："
                           f"{detail}"))
    else:
        items.append(_item("risk_open", "重大风险处理", True, True,
                           "无未处理的重大风险"))

    # 5. 授权与声明确认
    if rec.authorization_confirmed:
        items.append(_item("authorization", "授权与声明确认", True, True,
                           "已人工确认授权与相关声明"))
    else:
        items.append(_item("authorization", "授权与声明确认", False, True,
                           "授权与相关声明需要人工确认后才能发布"))

    # 6. 源拍摄项目（非阻断）
    if rec.work_id:
        work = db.execute(
            select(models.Work).where(
                models.Work.id == rec.work_id,
                models.Work.org_id == org_id)
        ).scalar_one_or_none()
        if work is None:
            items.append(_item("work_link", "源拍摄项目", False, False,
                               "关联的拍摄项目不存在（可能已被删除）"))
        else:
            items.append(_item("work_link", "源拍摄项目", True, False,
                               f"已关联：{work.title or work.id}"))
    else:
        items.append(_item("work_link", "源拍摄项目", True, False,
                           "未关联拍摄项目"))

    return items


def _blocking_passed(items: list[dict]) -> bool:
    return all(i["passed"] for i in items if i["blocking"])


def _rec_out(rec: models.PublishRecord) -> dict:
    return {
        "id": rec.id, "org_id": rec.org_id, "title": rec.title,
        "work_id": rec.work_id, "copy_version_id": rec.copy_version_id,
        "platform_version_id": rec.platform_version_id,
        "video_asset_id": rec.video_asset_id,
        "cover_asset_id": rec.cover_asset_id,
        "platform": rec.platform, "status": rec.status,
        "check_result": rec.check_result or [],
        "checked_at": rec.checked_at.isoformat() if rec.checked_at else None,
        "authorization_confirmed": bool(rec.authorization_confirmed),
        "published_at": (rec.published_at.isoformat()
                         if rec.published_at else None),
        "publish_url": rec.publish_url or "",
        "post_metrics": rec.post_metrics or {},
        "notes": rec.notes or "",
        "created_by": rec.created_by or "",
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
    }


def _platform_label(p: str) -> str:
    return {"douyin": "抖音", "kuaishou": "快手",
            "xiaohongshu": "小红书", "shipinhao": "视频号"}.get(p, p or "未指定")


def _status_label(s: str) -> str:
    return {
        models.PUBLISH_PENDING: "待准备", models.PUBLISH_REVIEW: "待审核",
        models.PUBLISH_READY: "待发布", models.PUBLISH_PUBLISHED: "已发布",
        models.PUBLISH_REVIEWED: "已复盘", models.PUBLISH_FAILED: "失败",
        models.PUBLISH_POSTPONED: "暂缓", models.PUBLISH_CANCELLED: "取消",
    }.get(s, s)


# ---------- 请求体 ----------

class PublishCreate(BaseModel):
    title: str = Field(default="", max_length=256)
    platform: str = Field(default="", max_length=32)
    work_id: Optional[str] = Field(default=None, max_length=32)
    copy_version_id: Optional[str] = Field(default=None, max_length=32)
    platform_version_id: Optional[str] = Field(default=None, max_length=32)
    video_asset_id: Optional[str] = Field(default=None, max_length=32)
    cover_asset_id: Optional[str] = Field(default=None, max_length=32)


class PublishUpdate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=256)
    platform: Optional[str] = Field(default=None, max_length=32)
    work_id: Optional[str] = Field(default=None, max_length=32)
    copy_version_id: Optional[str] = Field(default=None, max_length=32)
    platform_version_id: Optional[str] = Field(default=None, max_length=32)
    video_asset_id: Optional[str] = Field(default=None, max_length=32)
    cover_asset_id: Optional[str] = Field(default=None, max_length=32)
    authorization_confirmed: Optional[bool] = None
    published_at: Optional[str] = Field(default=None, max_length=64)
    publish_url: Optional[str] = Field(default=None, max_length=1024)
    notes: Optional[str] = None
    post_metrics: Optional[dict] = None


class PublishTransition(BaseModel):
    to_status: str = Field(..., max_length=16)
    published_at: Optional[str] = Field(default=None, max_length=64)
    publish_url: Optional[str] = Field(default=None, max_length=1024)


# ---------- 记录 CRUD ----------

@router.post("/records", status_code=status.HTTP_201_CREATED)
def create_record(req: PublishCreate,
                  org_id: str = Query(...),
                  db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    _require_publish_perm(db, user, org_id, "publish:create", "创建发布记录")
    if req.platform and req.platform not in models.PUBLISH_PLATFORMS:
        raise BusinessError(f"不支持的发布平台: {req.platform}",
                            code=error_codes.INVALID_PARAM, http_status=400)
    repo = PublishRepository(db)
    rec = repo.create(
        org_id, req.title, req.platform,
        work_id=req.work_id, copy_version_id=req.copy_version_id,
        platform_version_id=req.platform_version_id,
        video_asset_id=req.video_asset_id, cover_asset_id=req.cover_asset_id,
        created_by=user.id)
    return success(_rec_out(rec))


@router.get("/records")
def list_records(org_id: str = Query(...),
                 status_filter: Optional[str] = Query(default=None,
                                                     alias="status"),
                 db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    _require_publish_perm(db, user, org_id, "publish:read", "查看发布记录")
    if status_filter and status_filter not in models.PUBLISH_STATUSES:
        raise BusinessError(f"不支持的发布状态: {status_filter}",
                            code=error_codes.INVALID_PARAM, http_status=400)
    repo = PublishRepository(db)
    return success([_rec_out(r) for r in repo.list(org_id, status_filter)])


@router.get("/records/{rec_id}")
def get_record(rec_id: str,
               org_id: str = Query(...),
               db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    _require_publish_perm(db, user, org_id, "publish:read", "查看发布记录")
    repo = PublishRepository(db)
    return success(_rec_out(_rec_or_404(repo, org_id, rec_id)))


# 已发布/已复盘的记录：关联字段锁定，只允许改备注/数据/链接
_LOCKED_AFTER_PUBLISH = ("work_id", "copy_version_id", "platform_version_id",
                         "video_asset_id", "cover_asset_id", "platform")


@router.patch("/records/{rec_id}")
def update_record(rec_id: str, req: PublishUpdate,
                  org_id: str = Query(...),
                  db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    _require_publish_perm(db, user, org_id, "publish:update", "更新发布记录")
    repo = PublishRepository(db)
    rec = _rec_or_404(repo, org_id, rec_id)
    locked = rec.status in (models.PUBLISH_PUBLISHED, models.PUBLISH_REVIEWED)
    fields: dict[str, Any] = {}
    data = req.model_dump(exclude_unset=True)
    for k, v in data.items():
        if k in _LOCKED_AFTER_PUBLISH and locked:
            raise BusinessError("已发布的记录不允许修改关联内容",
                                code=error_codes.PUBLISH_INVALID_TRANSITION,
                                http_status=400)
        if k == "published_at" and v:
            try:
                v = datetime.fromisoformat(v)
            except ValueError:
                raise BusinessError("发布时间格式非法",
                                    code=error_codes.INVALID_PARAM,
                                    http_status=400)
        fields[k] = v
    if "platform" in fields and fields["platform"] \
            and fields["platform"] not in models.PUBLISH_PLATFORMS:
        raise BusinessError(f"不支持的发布平台: {fields['platform']}",
                            code=error_codes.INVALID_PARAM, http_status=400)
    repo.update_fields(rec, **fields)
    return success(_rec_out(rec))


@router.delete("/records/{rec_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_record(rec_id: str,
                  org_id: str = Query(...),
                  db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    _require_publish_perm(db, user, org_id, "publish:delete", "删除发布记录")
    repo = PublishRepository(db)
    rec = _rec_or_404(repo, org_id, rec_id)
    if rec.status in (models.PUBLISH_PUBLISHED, models.PUBLISH_REVIEWED):
        raise BusinessError("已发布/已复盘的记录不允许删除",
                            code=error_codes.PUBLISH_INVALID_TRANSITION,
                            http_status=400)
    repo.delete(org_id, rec_id)
    return None


# ---------- 发布前检查 ----------

@router.post("/records/{rec_id}/check")
def run_check(rec_id: str,
              org_id: str = Query(...),
              db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    _require_publish_perm(db, user, org_id, "publish:update", "运行发布前检查")
    repo = PublishRepository(db)
    rec = _rec_or_404(repo, org_id, rec_id)
    items = run_prepublish_check(db, org_id, rec)
    repo.update_fields(rec, check_result=items,
                       checked_at=datetime.now(timezone.utc))
    return success({"items": items,
                    "blocking_passed": _blocking_passed(items),
                    "checked_at": rec.checked_at.isoformat()})


# ---------- 状态流转 ----------

@router.post("/records/{rec_id}/transition")
def transition(rec_id: str, req: PublishTransition,
               org_id: str = Query(...),
               db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    _require_publish_perm(db, user, org_id, "publish:update", "更新发布状态")
    repo = PublishRepository(db)
    rec = _rec_or_404(repo, org_id, rec_id)
    to_status = req.to_status
    if to_status not in models.PUBLISH_STATUSES:
        raise BusinessError(f"不支持的发布状态: {to_status}",
                            code=error_codes.INVALID_PARAM, http_status=400)
    allowed = models.PUBLISH_TRANSITIONS.get(rec.status, ())
    if to_status not in allowed:
        raise BusinessError(
            f"不能从「{_status_label(rec.status)}」直接到"
            f"「{_status_label(to_status)}」",
            code=error_codes.PUBLISH_INVALID_TRANSITION, http_status=400)

    # review → ready：发布前检查必须全过
    if rec.status == models.PUBLISH_REVIEW \
            and to_status == models.PUBLISH_READY:
        items = run_prepublish_check(db, org_id, rec)
        repo.update_fields(rec, check_result=items,
                           checked_at=datetime.now(timezone.utc))
        if not _blocking_passed(items):
            failed = [i for i in items if i["blocking"] and not i["passed"]]
            raise BusinessError(
                "发布前检查未通过："
                + "；".join(i["label"] + "—" + i["detail"] for i in failed),
                code=error_codes.PUBLISH_CHECK_BLOCKED, http_status=400)

    # ready → published：必须填写发布时间和发布链接
    fields: dict[str, Any] = {"status": to_status}
    if to_status == models.PUBLISH_PUBLISHED:
        pub_at = req.published_at
        pub_url = (req.publish_url or "").strip()
        if not pub_at and not rec.published_at:
            raise BusinessError("请填写发布时间",
                                code=error_codes.PUBLISH_CHECK_BLOCKED,
                                http_status=400)
        if not pub_url and not (rec.publish_url or "").strip():
            raise BusinessError("请填写发布链接",
                                code=error_codes.PUBLISH_CHECK_BLOCKED,
                                http_status=400)
        if pub_at:
            try:
                fields["published_at"] = datetime.fromisoformat(pub_at)
            except ValueError:
                raise BusinessError("发布时间格式非法",
                                    code=error_codes.INVALID_PARAM,
                                    http_status=400)
        if pub_url:
            fields["publish_url"] = pub_url

    repo.update_fields(rec, **fields)
    return success(_rec_out(rec))


# ---------- 导出发布包 ----------

@router.get("/records/{rec_id}/export")
def export_package(rec_id: str,
                   org_id: str = Query(...),
                   db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """导出发布包 TXT（复制/下载用；仅支持人工发布流程）。"""
    _require_publish_perm(db, user, org_id, "publish:read", "导出发布包")
    repo = PublishRepository(db)
    rec = _rec_or_404(repo, org_id, rec_id)

    lines = [
        "鲁品开拍 · 发布包",
        "=" * 30,
        f"标题：{rec.title}",
        f"平台：{_platform_label(rec.platform)}",
        f"状态：{_status_label(rec.status)}",
    ]
    if rec.published_at:
        lines.append(f"发布时间：{rec.published_at.isoformat()}")
    if rec.publish_url:
        lines.append(f"发布链接：{rec.publish_url}")
    lines.append("")

    # 平台版本内容
    if rec.platform_version_id:
        pv = db.execute(
            select(models.PlatformVersion).where(
                models.PlatformVersion.id == rec.platform_version_id,
                models.PlatformVersion.org_id == org_id)
        ).scalar_one_or_none()
        if pv is not None:
            lines += [
                "-" * 30, "【发布文案】",
                f"标题：{pv.title or ''}",
                f"正文：{pv.body or ''}",
            ]
            if pv.hashtags:
                lines.append("话题：" + " ".join(pv.hashtags))
            if pv.cover_text:
                lines.append(f"封面文案：{pv.cover_text}")
            if pv.cta:
                lines.append(f"行动引导：{pv.cta}")
            if pv.pinned_comment:
                lines.append(f"置顶评论建议：{pv.pinned_comment}")
            lines.append("")

    # 文案版本原文
    if rec.copy_version_id:
        cv = db.execute(
            select(models.CopyVersion).where(
                models.CopyVersion.id == rec.copy_version_id,
                models.CopyVersion.org_id == org_id)
        ).scalar_one_or_none()
        if cv is not None:
            lines += ["-" * 30,
                      f"【源文案 v{cv.version_no}（{cv.status}）】",
                      cv.text or "", ""]

    # 检查清单
    lines += ["-" * 30, "【发布前检查】"]
    if rec.check_result:
        for i in rec.check_result:
            mark = "✓" if i.get("passed") else "✗"
            must = "（必须）" if i.get("blocking") else "（参考）"
            lines.append(
                f"{mark} {i.get('label')}{must} {i.get('detail') or ''}")
        if rec.checked_at:
            lines.append(f"检查时间：{rec.checked_at.isoformat()}")
    else:
        lines.append("尚未运行发布前检查")
    lines += ["", "注：本发布包仅用于人工复制发布，请手动完成各平台发布操作。"]

    return PlainTextResponse("\n".join(lines),
                             media_type="text/plain; charset=utf-8")
