"""RBAC 权限核心：角色定义、权限矩阵、检查函数。

角色（046）：owner（企业管理员）> admin > editor（编辑）
            > reviewer（审核员）> viewer（只读）

权限字符串命名：<资源>:<操作>，如 work:delete、fact:approve。

认证接口：使用认证组的 `CurrentUser`（Annotated[User, Depends(get_current_user)]），
在 handler 签名中直接写 `user: CurrentUser` 即可拿到 User 对象（含 .id）。
"""
from typing import Callable, Optional

from fastapi import Depends
from sqlalchemy.orm import Session

from .auth.deps import get_current_user
from .auth.database import get_db
from .auth.models import User
from . import models
from .core import error_codes
from .core.exceptions import AuthError, BusinessError

# ---------------------------------------------------------------------------
# 046：角色与权限矩阵
# ---------------------------------------------------------------------------

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_EDITOR = "editor"
ROLE_REVIEWER = "reviewer"
ROLE_VIEWER = "viewer"

VALID_ROLES = (ROLE_OWNER, ROLE_ADMIN, ROLE_EDITOR, ROLE_REVIEWER, ROLE_VIEWER)

# 角色等级（数字越大权限越高）
ROLE_RANK = {
    ROLE_VIEWER: 1,
    ROLE_REVIEWER: 2,
    ROLE_EDITOR: 3,
    ROLE_ADMIN: 4,
    ROLE_OWNER: 5,
}

# 全部权限点
ALL_PERMISSIONS = frozenset({
    # 组织
    "org:read", "org:update", "org:delete",
    "org:member:invite", "org:member:remove", "org:member:set_role",
    "org:config:model",          # 组织级模型密钥配置
    # 作品
    "work:create", "work:read", "work:update", "work:delete",
    "work:export",               # 导出全部数据（050 重要操作）
    # 事实库
    "fact:create", "fact:read", "fact:update", "fact:delete", "fact:approve",
    # 产品档案（V3.0 B-1：跨项目复用的产品资料中心）
    "product:create", "product:read", "product:update", "product:delete",
    # 文案优化器（V3.0 D：独立文案文档与版本）
    "copy:create", "copy:read", "copy:update", "copy:delete",
    # 内容策划工作台（V3.0 C）
    "plan:create", "plan:read", "plan:update", "plan:delete",
    # 发布中心（V3.0 F：发布记录与状态流转）
    "publish:create", "publish:read", "publish:update", "publish:delete",
    # 审核
    "review:submit", "review:approve",
    # 媒体资产（S8.1）
    "asset:upload", "asset:read", "asset:delete",
})

ROLE_PERMISSIONS: dict[str, frozenset] = {
    ROLE_OWNER: ALL_PERMISSIONS,
    ROLE_ADMIN: ALL_PERMISSIONS - {"org:delete"},
    ROLE_EDITOR: frozenset({
        "org:read",
        "work:create", "work:read", "work:update",
        "fact:create", "fact:read", "fact:update",
        "product:create", "product:read", "product:update",
        "copy:create", "copy:read", "copy:update",
        "plan:create", "plan:read", "plan:update",
        "publish:create", "publish:read", "publish:update",
        "review:submit",
        "asset:upload", "asset:read",
    }),
    ROLE_REVIEWER: frozenset({
        "org:read",
        "work:read",
        "fact:read", "fact:approve",
        "product:read",
        "copy:read",
        "plan:read",
        "publish:read",
        "review:submit", "review:approve",
        "asset:read",
    }),
    ROLE_VIEWER: frozenset({
        "org:read",
        "work:read",
        "fact:read",
        "product:read",
        "copy:read",
        "plan:read",
        "publish:read",
        "asset:read",
    }),
}

# ---------------------------------------------------------------------------
# 049：权限最小化硬限制
# 以下操作即使某天 ROLE_PERMISSIONS 被误配，也只允许 owner/admin 执行。
# ---------------------------------------------------------------------------
ORG_ADMIN_ONLY_ACTIONS = frozenset({
    "org:delete",            # 删除组织
    "org:member:set_role",   # 修改组织成员角色
    "org:member:remove",     # 移除成员
    "org:member:invite",     # 邀请成员
    "org:config:model",      # 修改组织级模型密钥配置
})


def has_permission(role: str, action: str) -> bool:
    """角色是否有某操作权限。049 硬限制优先于权限矩阵。"""
    if role not in VALID_ROLES:
        return False
    # 硬编码限制：组织管理类操作仅 owner/admin
    if action in ORG_ADMIN_ONLY_ACTIONS and ROLE_RANK[role] < ROLE_RANK[ROLE_ADMIN]:
        return False
    return action in ROLE_PERMISSIONS[role]


def get_membership(db: Session, user_id: str, org_id: str) -> Optional[models.Membership]:
    return (
        db.query(models.Membership)
        .filter(models.Membership.user_id == user_id,
                models.Membership.org_id == org_id)
        .first()
    )


def require_org_permission(action: str) -> Callable:
    """Depends 工厂：要求当前用户在目标组织内拥有指定权限。

    目标组织通过路径参数 org_id 传入。
    非成员或无权限 → 404（防枚举，不向无权用户确认组织存在）。
    """
    def _dep(
        org_id: str,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> models.Membership:
        org = db.query(models.Organization).filter(
            models.Organization.id == org_id).first()
        if org is None:
            raise BusinessError("组织不存在",
                                code=error_codes.ORG_NOT_FOUND, http_status=404)
        ms = get_membership(db, user.id, org_id)
        if ms is None or not has_permission(ms.role, action):
            raise BusinessError("组织不存在",
                                code=error_codes.ORG_NOT_FOUND, http_status=404)
        return ms
    return _dep


def get_work_or_404(db: Session, work_id: str) -> models.Work:
    work = db.query(models.Work).filter(models.Work.id == work_id).first()
    if work is None:
        raise BusinessError("作品不存在",
                            code=error_codes.WORK_NOT_FOUND, http_status=404)
    return work


def require_work_access(action: str) -> Callable:
    """047：对象级权限检查 Depends。

    从 works 表查出 work 的 org_id，验证当前用户是该组织成员
    且角色拥有对应权限。防止 IDOR/BOLA：
    - work 不存在 → 404
    - 用户非该组织成员或权限不足 → 404（不向无权用户确认资源存在）
    返回 (work, membership) 供业务 handler 使用。
    """
    def _dep(
        work_id: str,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> tuple:
        work = get_work_or_404(db, work_id)
        ms = get_membership(db, user.id, work.org_id)
        if ms is None or not has_permission(ms.role, action):
            raise BusinessError("作品不存在",
                                code=error_codes.WORK_NOT_FOUND, http_status=404)
        return work, ms
    return _dep


def assert_can(user_role: str, action: str, what: str = "执行此操作") -> None:
    """050：在 handler 内部重新验证重要操作的权限（不只靠路由 Depends）。"""
    if not has_permission(user_role, action):
        raise AuthError(f"权限不足，无法{what}", forbidden=True)
