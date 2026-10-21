"""作品 API（047/048/050/091/092/099）：租户隔离新体系。

027：全部接口使用标准响应信封 {"code":0,"data":...,"request_id":...}，
     response_model=ApiResponse[...]（删除类 204 无响应体除外）。
028：错误使用 error_codes 业务码，前端可按码展示。
031：作品 data 已类型化为 WorkData（见 schemas.py）。

- POST   /api/v1/works                       创建作品（必须指定 org_id，需 work:create）
- GET    /api/v1/works?org_id=...             列出某组织的作品（048：强制 org 上下文；092：排除回收站）
- GET    /api/v1/works/trash?org_id=...       092：回收站列表（需 work:delete 权限）
- GET    /api/v1/works/{work_id}             作品详情（047 对象级权限；092：回收站作品返回 404）
- PATCH  /api/v1/works/{work_id}             更新作品（047；091：data 变更前自动存快照）
- DELETE /api/v1/works/{work_id}             092：软删除进回收站；?hard=true 时 owner 且满 30 天可硬删除
- POST   /api/v1/works/{work_id}/restore     092：从回收站恢复
- GET    /api/v1/works/{work_id}/versions     091：历史版本列表
- POST   /api/v1/works/{work_id}/versions/{v}/restore  091：恢复到指定版本（先快照当前）
- GET    /api/v1/works/{work_id}/export-json 099：导出作品完整 JSON（含分镜、事实、版本历史）
- POST   /api/v1/works/{work_id}/export-all  导出全部数据（050：二次验证）

注意：FastAPI 按声明顺序匹配路由，/trash 与 /{id}/xxx 等固定路径
必须声明在 /{work_id} 之前，否则会被 {work_id} 吞掉。
"""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from ...auth.deps import get_current_user
from ...auth.models import User
from ...auth.database import get_db
from ... import models
from ...rbac import (
    get_membership, has_permission, require_work_access, assert_can,
    ROLE_OWNER,
)
from ...core import error_codes
from ...core.exceptions import AuthError, BusinessError
from ...core.response import ApiResponse, success
from ...security.audit import client_ip, log_audit as _audit
from .schemas import (
    WorkCreate, WorkUpdate, WorkOut, WorkVersionOut, TrashWorkOut,
    WorkData, WorkExportOut, WorkExportWork, WorkExportVersion, ExportAllOut,
)

router = APIRouter(prefix="/works", tags=["v1-works"])

# 091：单个作品最多保留快照数，超限删除最旧
MAX_VERSIONS_PER_WORK = 50


def _sync_product_ref(db, work) -> None:
    """V3.0 B-5：作品保存后同步产品引用标记。

    data_json.product_ref = {product_id, product_version, ...} 由前端在
    用产品档案生成内容后写入；后端只做标记，不读写作品内容。
    product_id 为空或产品不存在 → 清除旧标记。
    """
    from ...repositories.product_repository import ProductRepository
    try:
        data = json.loads(work.data_json or "{}")
    except (ValueError, TypeError):
        data = {}
    ref = (data.get("product_ref") or {}) if isinstance(data, dict) else {}
    repo = ProductRepository(db)
    pid = (ref.get("product_id") or "").strip() if isinstance(ref, dict) else ""
    if not pid:
        repo.sync_work_ref(work.id, None, 0)
        return
    product = repo.get(pid, work.org_id)
    if product is None:
        repo.sync_work_ref(work.id, None, 0)
        return
    try:
        pver = int(ref.get("product_version") or 0) or (product.version or 1)
    except (TypeError, ValueError):
        pver = product.version or 1
    repo.sync_work_ref(work.id, product, pver)


def _work_out(work: models.Work) -> WorkOut:
    try:
        data = json.loads(work.data_json or "{}")
    except (ValueError, TypeError):
        data = {}
    return WorkOut(
        id=work.id, org_id=work.org_id, owner_id=work.owner_id,
        title=work.title, data=data,
        status=work.status or "draft", version=work.version or 1,
        created_at=work.created_at, updated_at=work.updated_at,
    )


def _version_out(v: models.WorkVersion) -> WorkVersionOut:
    return WorkVersionOut(
        id=v.id, work_id=v.work_id, version_no=v.version_no,
        title=v.title, created_by=v.created_by, created_at=v.created_at,
    )


def _snapshot_work(db: Session, work: models.Work, user_id: str) -> models.WorkVersion:
    """091：修改前存快照。返回新版本记录。"""
    last = (db.query(models.WorkVersion)
            .filter(models.WorkVersion.work_id == work.id)
            .order_by(models.WorkVersion.version_no.desc())
            .first())
    next_no = (last.version_no + 1) if last else 1
    snap = models.WorkVersion(
        work_id=work.id,
        version_no=next_no,
        snapshot_json=work.data_json or "{}",
        title=work.title or "",
        created_by=user_id,
    )
    db.add(snap)
    db.flush()
    # 超限：删除最旧的快照（按 version_no 降序取，offset 之外的即最旧）
    excess = (db.query(models.WorkVersion)
              .filter(models.WorkVersion.work_id == work.id)
              .order_by(models.WorkVersion.version_no.desc())
              .offset(MAX_VERSIONS_PER_WORK)
              .all())
    for old in excess:
        db.delete(old)
    return snap


def _get_active_work(db: Session, work_id: str) -> models.Work:
    """取非回收站作品；回收站中的返回 404（092：正常业务流不可见已删除作品）。"""
    work = (db.query(models.Work)
            .filter(models.Work.id == work_id,
                    models.Work.deleted_at.is_(None))
            .first())
    if work is None:
        raise BusinessError("作品不存在",
                            code=error_codes.WORK_NOT_FOUND, http_status=404)
    return work


@router.post("", response_model=ApiResponse[WorkOut],
             status_code=status.HTTP_201_CREATED)
def create_work(
    req: WorkCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """创建作品。

    权限要求：登录用户；在目标组织内拥有 work:create 权限（048）。

    请求示例：
        POST /api/v1/works
        {"org_id": "org_123", "title": "章丘大葱宣传片",
         "data": {"product": {"product_name": "章丘大葱"}}}

    响应结构：标准信封，data 为 WorkOut（含类型化的 data.product/facts/storyboard）。

    错误码：
        4002 ORG_NOT_FOUND（组织不存在；无权限时同样返回 404 防枚举）
        4000 INVALID_PARAM（请求体校验失败，由 422 统一返回）
    """
    org = db.query(models.Organization).filter(
        models.Organization.id == req.org_id).first()
    if org is None:
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    ms = get_membership(db, user.id, req.org_id)
    if ms is None or not has_permission(ms.role, "work:create"):
        # 防枚举：与"组织不存在"返回一致
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    # S10：存储配额（超限 402）
    from ...services.quota_service import check_quota, QuotaExceededError
    try:
        check_quota(db, req.org_id, "storage_mb")
    except QuotaExceededError as e:
        raise BusinessError(str(e), code=error_codes.QUOTA_EXCEEDED,
                            http_status=402)
    work = models.Work(
        org_id=req.org_id,
        owner_id=user.id,
        title=req.title.strip(),
        data_json=json.dumps(req.data.model_dump(mode="json"), ensure_ascii=False),
    )
    db.add(work)
    db.flush()  # 先拿到 id，供产品引用标记
    # V3.0 B-5：同步产品引用标记（只标记，不读写内容）
    _sync_product_ref(db, work)
    db.commit()
    db.refresh(work)
    return success(_work_out(work))


@router.get("", response_model=ApiResponse[list[WorkOut]])
def list_works(
    org_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """列出某组织的作品。

    权限要求：登录用户；组织成员且拥有 work:read 权限（048 强制 org 上下文）。

    请求示例：
        GET /api/v1/works?org_id=org_123

    响应结构：标准信封，data 为 WorkOut 数组（092：排除回收站，按更新时间倒序）。

    错误码：
        4000 INVALID_PARAM（缺少 org_id 参数）
        4002 ORG_NOT_FOUND（组织不存在或无权限，防枚举）
    """
    if not org_id:
        raise BusinessError("必须指定 org_id",
                            code=error_codes.INVALID_PARAM, http_status=400)
    ms = get_membership(db, user.id, org_id)
    if ms is None or not has_permission(ms.role, "work:read"):
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    works = (
        db.query(models.Work)
        .filter(models.Work.org_id == org_id,          # 048：强制 org 过滤
                models.Work.deleted_at.is_(None))      # 092：排除回收站
        .order_by(models.Work.updated_at.desc())
        .all()
    )
    return success([_work_out(w) for w in works])


@router.get("/trash", response_model=ApiResponse[list[TrashWorkOut]])
def list_trash(
    org_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """092：回收站列表。需要 work:delete 权限（能删才能看回收站）。"""
    if not org_id:
        raise BusinessError("必须指定 org_id",
                            code=error_codes.INVALID_PARAM, http_status=400)
    ms = get_membership(db, user.id, org_id)
    if ms is None or not has_permission(ms.role, "work:delete"):
        raise BusinessError("组织不存在",
                            code=error_codes.ORG_NOT_FOUND, http_status=404)
    works = (
        db.query(models.Work)
        .filter(models.Work.org_id == org_id,
                models.Work.deleted_at.isnot(None))
        .order_by(models.Work.deleted_at.desc())
        .all()
    )
    out = []
    for w in works:
        wo = _work_out(w)
        out.append(TrashWorkOut(**wo.model_dump(mode="json"), deleted_at=w.deleted_at))
    return success(out)


@router.get("/{work_id}", response_model=ApiResponse[WorkOut])
def get_work(
    access: tuple = Depends(require_work_access("work:read")),
    db: Session = Depends(get_db),
):
    """获取作品详情。

    权限要求：登录用户；作品所属组织成员且拥有 work:read（047 对象级权限）。

    请求示例：
        GET /api/v1/works/wk_abc123

    响应结构：标准信封，data 为 WorkOut。

    错误码：
        1001 WORK_NOT_FOUND（作品不存在 / 无权限 / 在回收站，防枚举统一 404）
    """
    work, _ms = access
    # 092：回收站作品对正常详情接口不可见
    work = _get_active_work(db, work.id)
    out = _work_out(work)
    # V3.0 B-5：产品引用是否已过期（只做标记展示，不改写内容）
    from ...repositories.product_repository import ProductRepository
    out.product_ref_stale = ProductRepository(db).is_ref_stale("work", work.id)
    return success(out)


@router.patch("/{work_id}", response_model=ApiResponse[WorkOut])
def update_work(
    work_id: str,
    req: WorkUpdate,
    access: tuple = Depends(require_work_access("work:update")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """更新作品（标题 / 文档 / 状态）。

    权限要求：登录用户；组织成员且拥有 work:update（047）。

    请求示例：
        PATCH /api/v1/works/wk_abc123
        {"title": "新标题", "version": 3,
         "data": {"product": {"product_name": "章丘大葱"}}}

    说明：
        - 091：data 实质变更前自动存快照（仅改标题不触发）；
        - 088：携带 version 时做乐观锁检查，冲突返回 409；
        - data 为全量替换语义（031：WorkData 类型化）。

    响应结构：标准信封，data 为更新后的 WorkOut。

    错误码：
        1001 WORK_NOT_FOUND（作品不存在 / 在回收站）
        4090 VERSION_CONFLICT（版本冲突，请刷新后重试）
        1004 INVALID_WORK_STATUS（非法 status，仅允许 draft/active/archived）
    """
    work, _ms = access
    # 重新从当前会话加载以获得可写对象（Depends 内会话已关闭）
    work = _get_active_work(db, work_id)
    # 088：带 version 时先做乐观锁检查（在快照之前，避免冲突产生脏快照）；
    # 行锁保证检查-更新原子性（PostgreSQL 生效，SQLite 写串行化）。
    if req.version is not None:
        work = (db.query(models.Work)
                .filter(models.Work.id == work_id,
                        models.Work.deleted_at.is_(None))
                .with_for_update()
                .first())
        if work is None:
            raise BusinessError("作品不存在",
                                code=error_codes.WORK_NOT_FOUND, http_status=404)
        if work.version != req.version:
            raise BusinessError(
                f"作品已被他人修改（当前版本 {work.version}），请刷新后重试",
                code=error_codes.VERSION_CONFLICT, http_status=409)
    # 091：data 实质变更前先存快照（仅改标题不触发快照）
    if req.data is not None:
        _snapshot_work(db, work, user.id)
        work.data_json = json.dumps(req.data.model_dump(mode="json"),
                                    ensure_ascii=False)
    if req.title is not None:
        work.title = req.title.strip()
    if req.status is not None:
        if req.status not in ("draft", "active", "archived"):
            raise BusinessError("非法 status",
                                code=error_codes.INVALID_WORK_STATUS,
                                http_status=422)
        work.status = req.status
    if req.version is not None:
        work.version = work.version + 1
    # V3.0 B-5：同步产品引用标记（只标记，不读写内容）
    _sync_product_ref(db, work)
    db.commit()
    db.refresh(work)
    return success(_work_out(work))


@router.delete("/{work_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_work(
    work_id: str,
    request: Request,
    hard: bool = Query(default=False, description="硬删除：仅 owner 且回收站满 30 天"),
    access: tuple = Depends(require_work_access("work:delete")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """删除作品（092：默认软删除进回收站，可恢复）。

    权限要求：登录用户；组织成员且拥有 work:delete；handler 内二次验证（050）。

    请求示例：
        DELETE /api/v1/works/wk_abc123
        DELETE /api/v1/works/wk_abc123?hard=true

    响应：204 无内容（删除类接口不套信封）。

    错误码：
        1001 WORK_NOT_FOUND（作品不存在）
        4001 PERMISSION_DENIED（硬删除非 owner 执行）
        4000 INVALID_PARAM（不在回收站直接硬删除 / 未满 30 天）
    """
    work, ms = access
    assert_can(ms.role, "work:delete", "删除作品")  # 050 二次验证
    work = db.query(models.Work).filter(models.Work.id == work_id).first()
    if work is None:  # 已被硬删除的极端竞态
        raise BusinessError("作品不存在",
                            code=error_codes.WORK_NOT_FOUND, http_status=404)

    if hard:
        if ms.role != ROLE_OWNER:
            raise AuthError("硬删除只允许组织 owner 执行", forbidden=True)
        if work.deleted_at is None:
            raise BusinessError("作品不在回收站，不能直接硬删除（请先软删除）",
                                code=error_codes.INVALID_PARAM, http_status=400)
        deleted_at = work.deleted_at
        if deleted_at.tzinfo is None:
            # SQLite 存入的 datetime 可能丢失 tzinfo，统一按 UTC 处理
            deleted_at = deleted_at.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - deleted_at).days < 30:
            raise BusinessError("回收站未满 30 天，不能硬删除（到期由清理任务自动处理）",
                                code=error_codes.INVALID_PARAM, http_status=400)
        db.query(models.WorkVersion).filter(
            models.WorkVersion.work_id == work.id).delete()
        # Phase 9-5：硬删除写审计
        _audit(db, action="work.hard_delete", org_id=work.org_id,
               user_id=user.id, object_type="work", object_id=work.id,
               detail={"title": work.title}, ip=client_ip(request))
        db.delete(work)
        db.commit()
        return None

    if work.deleted_at is not None:
        # 已在回收站：幂等返回
        return None
    work.deleted_at = datetime.now(timezone.utc)
    # Phase 9-5：软删除写审计
    _audit(db, action="work.delete", org_id=work.org_id,
           user_id=user.id, object_type="work", object_id=work.id,
           detail={"title": work.title}, ip=client_ip(request))
    db.commit()
    return None


@router.post("/{work_id}/restore", response_model=ApiResponse[WorkOut])
def restore_work(
    work_id: str,
    access: tuple = Depends(require_work_access("work:delete")),
    db: Session = Depends(get_db),
):
    """从回收站恢复作品（092）。

    权限要求：登录用户；组织成员且拥有 work:delete 权限。

    请求示例：
        POST /api/v1/works/wk_abc123/restore

    响应结构：标准信封，data 为恢复后的 WorkOut。

    错误码：
        1001 WORK_NOT_FOUND（回收站中没有该作品）
        4001 PERMISSION_DENIED（无 work:delete 权限）
    """
    work, ms = access
    assert_can(ms.role, "work:delete", "恢复作品")
    work = db.query(models.Work).filter(models.Work.id == work_id).first()
    if work is None or work.deleted_at is None:
        raise BusinessError("回收站中没有该作品",
                            code=error_codes.WORK_NOT_FOUND, http_status=404)
    work.deleted_at = None
    db.commit()
    db.refresh(work)
    return success(_work_out(work))


@router.get("/{work_id}/versions", response_model=ApiResponse[list[WorkVersionOut]])
def list_versions(
    work_id: str,
    access: tuple = Depends(require_work_access("work:read")),
    db: Session = Depends(get_db),
):
    """获取作品历史版本列表（091）。

    权限要求：登录用户；组织成员且拥有 work:read。

    请求示例：
        GET /api/v1/works/wk_abc123/versions

    响应结构：标准信封，data 为 WorkVersionOut 数组（版本号倒序）。

    错误码：
        1001 WORK_NOT_FOUND（作品不存在 / 在回收站）
    """
    work, _ms = access
    _get_active_work(db, work.id)
    versions = (db.query(models.WorkVersion)
                .filter(models.WorkVersion.work_id == work_id)
                .order_by(models.WorkVersion.version_no.desc())
                .all())
    return success([_version_out(v) for v in versions])


@router.post("/{work_id}/versions/{version_no}/restore",
             response_model=ApiResponse[WorkOut])
def restore_version(
    work_id: str,
    version_no: int,
    access: tuple = Depends(require_work_access("work:update")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """091：恢复到指定版本。恢复前先把当前状态存为快照（恢复操作本身可撤销）。"""
    work, _ms = access
    work = _get_active_work(db, work_id)
    ver = (db.query(models.WorkVersion)
           .filter(models.WorkVersion.work_id == work_id,
                   models.WorkVersion.version_no == version_no)
           .first())
    if ver is None:
        raise BusinessError("版本不存在",
                            code=error_codes.WORK_VERSION_NOT_FOUND,
                            http_status=404)
    _snapshot_work(db, work, user.id)  # 当前状态先存档
    work.data_json = ver.snapshot_json
    work.title = ver.title
    db.commit()
    db.refresh(work)
    return success(_work_out(work))


@router.get("/{work_id}/export-json", response_model=ApiResponse[WorkExportOut])
def export_json(
    work_id: str,
    access: tuple = Depends(require_work_access("work:export")),
    db: Session = Depends(get_db),
):
    """导出作品完整 JSON（099：备份/迁移用）。

    权限要求：登录用户；组织成员且拥有 work:export；二次验证（050）。

    请求示例：
        GET /api/v1/works/wk_abc123/export-json

    响应结构：标准信封，data 为 WorkExportOut
        {format, format_version, exported_at, work{...data}, versions[...]}。

    错误码：
        1001 WORK_NOT_FOUND（作品不存在 / 在回收站）
        4001 PERMISSION_DENIED（无导出权限）
    """
    work, ms = access
    assert_can(ms.role, "work:export", "导出作品数据")
    work = db.query(models.Work).filter(models.Work.id == work_id).first()
    if work is None or work.deleted_at is not None:
        raise BusinessError("作品不存在",
                            code=error_codes.WORK_NOT_FOUND, http_status=404)
    try:
        data = json.loads(work.data_json or "{}")
    except (ValueError, TypeError):
        data = {}
    versions = (db.query(models.WorkVersion)
                .filter(models.WorkVersion.work_id == work_id)
                .order_by(models.WorkVersion.version_no.asc())
                .all())

    def _snap(v) -> WorkExportVersion:
        try:
            snap = json.loads(v.snapshot_json or "{}")
        except (ValueError, TypeError):
            snap = {}
        return WorkExportVersion(
            version_no=v.version_no, title=v.title or "",
            created_by=v.created_by, created_at=v.created_at,
            snapshot=snap)

    out = WorkExportOut(
        exported_at=datetime.now(timezone.utc),
        work=WorkExportWork(
            id=work.id, org_id=work.org_id, owner_id=work.owner_id,
            title=work.title or "", created_at=work.created_at,
            updated_at=work.updated_at, data=data),
        versions=[_snap(v) for v in versions],
    )
    return success(out)


@router.post("/{work_id}/export-all", response_model=ApiResponse[ExportAllOut])
def export_all(
    work_id: str,
    access: tuple = Depends(require_work_access("work:export")),
    db: Session = Depends(get_db),
):
    """050：导出全部数据是重要操作——二次验证权限，不只靠前端按钮隐藏。"""
    work, ms = access
    assert_can(ms.role, "work:export", "导出全部数据")  # 050 二次验证
    work = _get_active_work(db, work.id)
    return success(ExportAllOut(work=_work_out(work), exported_by_role=ms.role))
