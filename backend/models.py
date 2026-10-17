"""SQLAlchemy ORM 模型：组织 / 成员 / 邀请 / 作品 / 分镜 / 镜头 / 事实库。

与认证组共用 Base（backend.auth.models），同一 metadata 下
`alembic upgrade head` 建出全部表（本地开发也可用 create_all 兜底）。

Phase 3（081-088）变更：
- 083：works 补 status（draft/active/archived）与 deleted_at（软删除预留）
- 084：storyboards / shots 表；shots 以 UUID 为 PK（稳定 ID），shot_no 仅展示顺序
- 085：facts 事实库表（pending/confirmed/rejected/expired + allow_use + 有效期）
- 086：全部 DB 级外键；子表 ondelete=CASCADE；works.org_id 用 RESTRICT
      防止误删组织时连带清空租户资产（需先显式删除作品）
- 088：works / storyboards / facts 加 version 乐观锁字段

注意：User 表由认证组负责；user_id / owner_id / created_by / verified_by
均为字符串逻辑关联（User.id 是 str），不做 DB 级外键，避免模块耦合。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from .auth.models import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Organization(Base):
    """企业组织。045"""
    __tablename__ = "organizations"

    id = Column(String(32), primary_key=True, default=_uuid)
    name = Column(String(128), nullable=False)
    created_by = Column(String(64), nullable=False)  # 创建者 user_id（逻辑关联）
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class Membership(Base):
    """组织成员关系：一个用户可加入多个组织。045"""
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "org_id", name="uq_membership_user_org"),
        Index("ix_membership_org", "org_id"),
        Index("ix_membership_user", "user_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    user_id = Column(String(64), nullable=False)  # 逻辑关联 users.id
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="CASCADE"),
                    nullable=False)
    # owner | admin | editor | reviewer | viewer
    role = Column(String(16), nullable=False, default="viewer")
    joined_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class Invitation(Base):
    """组织邀请码：7 天有效。045"""
    __tablename__ = "invitations"
    __table_args__ = (
        Index("ix_invitation_code", "code", unique=True),
        Index("ix_invitation_org", "org_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="CASCADE"),
                    nullable=False)
    code = Column(String(24), nullable=False, unique=True)
    # 被邀请者加入后的默认角色
    role = Column(String(16), nullable=False, default="viewer")
    created_by = Column(String(64), nullable=False)  # 逻辑关联 users.id
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_by = Column(String(64), nullable=True)


class Work(Base):
    """作品：租户隔离。083/088：状态、软删除、乐观锁版本。"""
    __tablename__ = "works"
    __table_args__ = (
        Index("ix_work_org", "org_id"),
        Index("ix_work_owner", "owner_id"),
        Index("ix_work_status", "status"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    # 086：组织删除时先显式处理作品，RESTRICT 防止误删连带清空
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="RESTRICT"),
                    nullable=False)
    owner_id = Column(String(64), nullable=False)  # 逻辑关联 users.id
    title = Column(String(256), nullable=False, default="")
    data_json = Column(Text, nullable=False, default="{}")
    # 083：draft | active | archived
    status = Column(String(16), nullable=False, default="draft", server_default="draft")
    # 083：软删除时间戳（NULL=未删除）；删除 API 后续改为写此字段
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    # 088：乐观锁版本，每次更新 +1
    version = Column(Integer, nullable=False, default=1, server_default="1")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


class Storyboard(Base):
    """084：分镜版本。一个作品可有多个分镜版本，version 做乐观锁。"""
    __tablename__ = "storyboards"
    __table_args__ = (
        Index("ix_storyboard_work", "work_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    work_id = Column(String(32), ForeignKey("works.id", ondelete="CASCADE"),
                     nullable=False)
    version = Column(Integer, nullable=False, default=1, server_default="1")
    title = Column(String(256), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


class Shot(Base):
    """084：镜头。id 为稳定 UUID（跨排序/重生成保持关联）；
    shot_no 只是展示顺序。shot_id_stable 承接前端已有稳定 ID。"""
    __tablename__ = "shots"
    __table_args__ = (
        Index("ix_shot_storyboard", "storyboard_id"),
        Index("ix_shot_stable", "shot_id_stable"),
        UniqueConstraint("storyboard_id", "shot_id_stable",
                         name="uq_shot_storyboard_stable"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    storyboard_id = Column(String(32), ForeignKey("storyboards.id", ondelete="CASCADE"),
                           nullable=False)
    shot_no = Column(Integer, nullable=False, default=1)
    shot_id_stable = Column(String(64), nullable=True)
    content_json = Column(JSON, nullable=False, default=dict)
    duration_seconds = Column(Integer, nullable=False, default=0)
    # 032：镜头锁定状态——锁定镜头不许改动，由 StoryboardRepository.set_shot_lock()
    # 在事务中修改
    locked = Column(Boolean, nullable=False, default=False, server_default="0")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


# 085：事实状态
# S6.1：六态工作流 draft -> pending_review -> confirmed -> expired/revoked，
# 另有 rejected。历史值 "pending"（085 时代）视为 pending_review 别名，
# 0005 迁移已把存量改写；此处保留常量供读兼容。
# 注意：字符串取值必须与 backend/facts/state_machine.py 保持一致
#（两处为避免循环导入而各自定义，请同步修改）。
FACT_STATUS_DRAFT = "draft"
FACT_STATUS_PENDING = "pending"  # 历史别名 == pending_review
FACT_STATUS_PENDING_REVIEW = "pending_review"
FACT_STATUS_CONFIRMED = "confirmed"
FACT_STATUS_REJECTED = "rejected"
FACT_STATUS_EXPIRED = "expired"
FACT_STATUS_REVOKED = "revoked"
FACT_STATUSES = (
    FACT_STATUS_DRAFT, FACT_STATUS_PENDING, FACT_STATUS_PENDING_REVIEW,
    FACT_STATUS_CONFIRMED, FACT_STATUS_REJECTED, FACT_STATUS_EXPIRED,
    FACT_STATUS_REVOKED,
)
# AI 可引用的状态（010：还需 allow_use=true 且未过期）
FACT_USABLE_STATUSES = (FACT_STATUS_CONFIRMED,)


class Fact(Base):
    """085：可信事实库。AI 只引用 status=confirmed 且 allow_use 的事实（010）；
    事实本身的状态流转只能由人工/确定性规则推进，模型无权自升（143）。"""
    __tablename__ = "facts"
    __table_args__ = (
        Index("ix_fact_org", "org_id"),
        Index("ix_fact_product", "product_id"),
        Index("ix_fact_status", "status"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="CASCADE"),
                    nullable=False)
    product_id = Column(String(64), nullable=False, default="")
    content = Column(Text, nullable=False)
    category = Column(String(64), nullable=False, default="")
    # official_document | enterprise_material | human_statement | ai_inference
    source_type = Column(String(32), nullable=False, default="human_statement")
    source_ref = Column(Text, nullable=False, default="")
    status = Column(String(16), nullable=False, default=FACT_STATUS_DRAFT,
                    server_default=FACT_STATUS_DRAFT)
    allow_use = Column(Boolean, nullable=False, default=False, server_default="0")
    verified_by = Column(String(64), nullable=True)  # 逻辑关联 users.id
    verified_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    # 088：乐观锁版本
    version = Column(Integer, nullable=False, default=1, server_default="1")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


class FactStatusHistory(Base):
    """S6.1：事实状态变更审计。

    每次状态流转必须记录操作者、时间、原因和证据引用；
    确认/驳回/撤销缺 reason 拒绝流转（state_machine.REASON_REQUIRED）。
    """
    __tablename__ = "fact_status_history"
    __table_args__ = (
        Index("ix_facthist_fact", "fact_id"),
        Index("ix_facthist_org", "org_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    fact_id = Column(String(32), ForeignKey("facts.id", ondelete="CASCADE"),
                     nullable=False)
    org_id = Column(String(32), nullable=False)  # 冗余租户字段，便于按组织审计
    from_status = Column(String(16), nullable=False)
    to_status = Column(String(16), nullable=False)
    operator_id = Column(String(64), nullable=False, default="")  # 逻辑关联 users.id；"system" 表系统自动
    reason = Column(Text, nullable=False, default="")
    evidence_ref = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class FactVersion(Base):
    """S6.1：事实内容版本快照（与 work_versions 091 同模式）。

    事实内容（content/category/source_type/source_ref）变更前存一条，
    version_no 单调递增；超限删最旧（上限 50）。
    """
    __tablename__ = "fact_versions"
    __table_args__ = (
        Index("ix_factversion_fact", "fact_id"),
        UniqueConstraint("fact_id", "version_no", name="uq_factversion_no"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    fact_id = Column(String(32), ForeignKey("facts.id", ondelete="CASCADE"),
                     nullable=False)
    version_no = Column(Integer, nullable=False)
    content = Column(Text, nullable=False, default="")
    category = Column(String(64), nullable=False, default="")
    source_type = Column(String(32), nullable=False, default="")
    source_ref = Column(Text, nullable=False, default="")
    status = Column(String(16), nullable=False, default="")
    allow_use = Column(Boolean, nullable=False, default=False)
    created_by = Column(String(64), nullable=False, default="")  # 逻辑关联 users.id
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class WorkVersion(Base):
    """091：作品历史快照（兄弟组 API 已引用，此处补模型定义）。

    每次 data 实质变更前存一条，version_no 单调递增；超限删最旧。
    """
    __tablename__ = "work_versions"
    __table_args__ = (
        Index("ix_workversion_work", "work_id"),
        UniqueConstraint("work_id", "version_no", name="uq_workversion_no"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    work_id = Column(String(32), ForeignKey("works.id", ondelete="CASCADE"),
                     nullable=False)
    version_no = Column(Integer, nullable=False)
    snapshot_json = Column(Text, nullable=False, default="{}")
    title = Column(String(256), nullable=False, default="")
    created_by = Column(String(64), nullable=False, default="")  # 逻辑关联 users.id
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


# 032/201：审核状态机——区分"未查看"与"已批准"，审核结论绑定内容版本。
REVIEW_STATUS_PENDING = "pending"            # 未提交
REVIEW_STATUS_IN_REVIEW = "in_review"       # 审核中
REVIEW_STATUS_CHANGES = "changes_requested"  # 需修改
REVIEW_STATUS_APPROVED = "approved"          # 已通过
REVIEW_STATUS_REJECTED = "rejected"          # 已驳回
REVIEW_STATUS_EXPIRED = "expired"            # 已过期（内容变更后自动失效）
REVIEW_STATUSES = (
    REVIEW_STATUS_PENDING, REVIEW_STATUS_IN_REVIEW, REVIEW_STATUS_CHANGES,
    REVIEW_STATUS_APPROVED, REVIEW_STATUS_REJECTED, REVIEW_STATUS_EXPIRED,
)
# 合法流转（032：非法流转在 ReviewRepository.transition() 拒绝）
REVIEW_TRANSITIONS: dict[str, tuple[str, ...]] = {
    REVIEW_STATUS_PENDING: (REVIEW_STATUS_IN_REVIEW, REVIEW_STATUS_EXPIRED),
    REVIEW_STATUS_IN_REVIEW: (
        REVIEW_STATUS_APPROVED, REVIEW_STATUS_REJECTED,
        REVIEW_STATUS_CHANGES, REVIEW_STATUS_EXPIRED,
    ),
    REVIEW_STATUS_CHANGES: (REVIEW_STATUS_IN_REVIEW, REVIEW_STATUS_EXPIRED),
    REVIEW_STATUS_APPROVED: (REVIEW_STATUS_EXPIRED,),
    REVIEW_STATUS_REJECTED: (REVIEW_STATUS_IN_REVIEW, REVIEW_STATUS_EXPIRED),
    REVIEW_STATUS_EXPIRED: (REVIEW_STATUS_IN_REVIEW,),
}


class Review(Base):
    """032/201：内容审核记录。

    - 203：审核结论绑定 content_hash（分镜版本哈希），内容变则自动 expired。
    - 一条作品同一时刻只有一条"有效"审核（pending/in_review/approved），
      历史结论保留在表中做审计（213）。
    """
    __tablename__ = "reviews"
    __table_args__ = (
        Index("ix_review_work", "work_id"),
        Index("ix_review_status", "status"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="RESTRICT"),
                    nullable=False)
    work_id = Column(String(32), ForeignKey("works.id", ondelete="CASCADE"),
                     nullable=False)
    storyboard_id = Column(String(32), ForeignKey("storyboards.id", ondelete="SET NULL"),
                           nullable=True)
    # 203：被审核内容的内容哈希（分镜 version + 镜头快照哈希）
    content_hash = Column(String(64), nullable=False, default="")
    # S6.3：审核绑定规则/模型/Prompt 版本，任一变更都应触发重新审核
    rule_version = Column(String(64), nullable=False, default="")
    model_version = Column(String(128), nullable=False, default="")
    prompt_version = Column(String(64), nullable=False, default="")
    status = Column(String(16), nullable=False, default=REVIEW_STATUS_PENDING,
                    server_default=REVIEW_STATUS_PENDING)
    # 205：每项风险的处置结果 {risk_id: retained|modified|removed|escalated}
    decisions_json = Column(JSON, nullable=False, default=dict)
    reviewer_id = Column(String(64), nullable=True)   # 逻辑关联 users.id
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    comment = Column(Text, nullable=False, default="")
    # 088 风格乐观锁
    version = Column(Integer, nullable=False, default=1, server_default="1")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


# S6.5：交付门禁类型
GATE_TYPE_PLAN = "plan"
GATE_TYPE_PRODUCTION = "production"
GATE_TYPE_FINAL = "final"
GATE_TYPES = (GATE_TYPE_PLAN, GATE_TYPE_PRODUCTION, GATE_TYPE_FINAL)


class DeliveryGateConfig(Base):
    """S6.5：交付门禁配置——每种交付类型下各检查项是否为阻断项。

    取代 delivery_service 里写死的 blocking 规则；默认种子数据与旧硬编码
    行为完全一致，老 /api/delivery-check 接口不受影响。
    """
    __tablename__ = "delivery_gate_configs"
    __table_args__ = (
        Index("ix_gate_type", "gate_type"),
        UniqueConstraint("gate_type", "check_id", name="uq_gate_check"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    gate_type = Column(String(16), nullable=False)          # plan/production/final
    check_id = Column(String(32), nullable=False)          # product/facts/...
    label = Column(String(128), nullable=False, default="")
    is_blocking = Column(Boolean, nullable=False, default=True,
                         server_default="1")
    is_waivable = Column(Boolean, nullable=False, default=False,
                         server_default="0")
    # 条件阻断（如 feasibility：平时建议项，但发现不可执行镜头时变阻断）
    conditional_blocking = Column(Boolean, nullable=False, default=False,
                                  server_default="0")
    required_permission = Column(String(64), nullable=False, default="")
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    updated_by = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


class Waiver(Base):
    """S6.5：交付门禁豁免——阻断项可豁免，但必须记录原因和批准人。"""
    __tablename__ = "waivers"
    __table_args__ = (
        Index("ix_waiver_work", "work_id"),
        Index("ix_waiver_org", "org_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="RESTRICT"),
                    nullable=False)
    work_id = Column(String(32), ForeignKey("works.id", ondelete="CASCADE"),
                     nullable=False)
    gate_type = Column(String(16), nullable=False)
    check_id = Column(String(32), nullable=False)
    reason = Column(Text, nullable=False, default="")
    approved_by = Column(String(64), nullable=False)   # 逻辑关联 users.id
    expires_at = Column(DateTime(timezone=True), nullable=True)
    revoked = Column(Boolean, nullable=False, default=False, server_default="0")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


# S6.4：风险发现严重程度 / 处置状态
FINDING_SEV_HIGH = "high"
FINDING_SEV_MEDIUM = "medium"
FINDING_SEV_LOW = "low"
FINDING_SEVERITIES = (FINDING_SEV_HIGH, FINDING_SEV_MEDIUM, FINDING_SEV_LOW)

FINDING_STATUS_OPEN = "open"        # 待处理
FINDING_STATUS_FIXED = "fixed"      # 已修复
FINDING_STATUS_WAIVED = "waived"    # 已豁免（人工接受风险）
FINDING_STATUSES = (FINDING_STATUS_OPEN, FINDING_STATUS_FIXED,
                    FINDING_STATUS_WAIVED)


class RiskFinding(Base):
    """S6.4：风险发现追踪——每条风险定位到镜头或文字片段，跟踪解决状态。"""
    __tablename__ = "risk_findings"
    __table_args__ = (
        Index("ix_finding_review", "review_id"),
        Index("ix_finding_work", "work_id"),
        Index("ix_finding_status", "status"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    review_id = Column(String(32), ForeignKey("reviews.id", ondelete="CASCADE"),
                       nullable=False)
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="RESTRICT"),
                    nullable=False)
    work_id = Column(String(32), ForeignKey("works.id", ondelete="CASCADE"),
                     nullable=False)
    # 定位：镜头稳定 ID（可空）+ 文字片段（可空），至少其一
    shot_id = Column(String(64), nullable=True)
    shot_no = Column(Integer, nullable=True)
    text_fragment = Column(Text, nullable=True)
    severity = Column(String(16), nullable=False, default=FINDING_SEV_MEDIUM)
    status = Column(String(16), nullable=False, default=FINDING_STATUS_OPEN,
                    server_default=FINDING_STATUS_OPEN)
    source = Column(String(64), nullable=False, default="")  # AI 风险识别/事实依据检查
    reason = Column(Text, nullable=False, default="")
    resolved_by = Column(String(64), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


# 035/036：AI 异步任务表
TASK_TYPE_IDEAS = "ideas"
TASK_TYPE_STORYBOARD = "storyboard"
TASK_TYPE_REGEN_SHOT = "regen_shot"
TASK_TYPE_RISK = "risk_check"
TASK_TYPE_FEASIBILITY = "feasibility"
TASK_TYPES = (
    TASK_TYPE_IDEAS, TASK_TYPE_STORYBOARD, TASK_TYPE_REGEN_SHOT,
    TASK_TYPE_RISK, TASK_TYPE_FEASIBILITY,
)

TASK_STATUS_PENDING = "pending"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCEEDED = "succeeded"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_CANCELLED = "cancelled"
TASK_STATUSES = (
    TASK_STATUS_PENDING, TASK_STATUS_RUNNING, TASK_STATUS_SUCCEEDED,
    TASK_STATUS_FAILED, TASK_STATUS_CANCELLED,
)
# 036：合法状态转换；终态不允许再转
TASK_TRANSITIONS: dict[str, tuple[str, ...]] = {
    TASK_STATUS_PENDING: (TASK_STATUS_RUNNING, TASK_STATUS_CANCELLED),
    TASK_STATUS_RUNNING: (
        TASK_STATUS_SUCCEEDED, TASK_STATUS_FAILED, TASK_STATUS_CANCELLED,
    ),
    TASK_STATUS_SUCCEEDED: (),
    TASK_STATUS_FAILED: (),
    TASK_STATUS_CANCELLED: (),
}


class AITask(Base):
    """035：AI 异步任务。ThreadPoolExecutor 执行，结果落库，前端轮询。

    036：status 流转必须走 validate_task_transition()，非法抛 IllegalTaskTransition。
    """
    __tablename__ = "ai_tasks"
    __table_args__ = (
        Index("ix_aitask_org", "org_id"),
        Index("ix_aitask_status", "status"),
        Index("ix_aitask_created", "created_at"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="RESTRICT"),
                    nullable=False)
    created_by = Column(String(64), nullable=False, default="")  # 逻辑关联 users.id
    task_type = Column(String(32), nullable=False)
    status = Column(String(16), nullable=False, default=TASK_STATUS_PENDING,
                    server_default=TASK_STATUS_PENDING)
    # 请求参数（脱敏后存：不存 API Key）
    payload_json = Column(JSON, nullable=False, default=dict)
    # 成功结果
    result_json = Column(JSON, nullable=True)
    # 失败原因 / 取消原因
    error = Column(Text, nullable=True)
    # 0-100；running 阶段更新
    progress = Column(Integer, nullable=False, default=0, server_default="0")
    # 实际使用的模型（122：记录实际生效配置，防静默回退）
    model_used = Column(String(128), nullable=True)
    # 协作式取消标记：running 任务由工作线程轮询此标记
    cancel_requested = Column(Boolean, nullable=False, default=False,
                              server_default="0")
    # 137：请求优先级 high（用户交互）/ low（后台批量），队列优先执行 high
    priority = Column(String(8), nullable=False, default="high",
                      server_default="high")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)


class AIBudget(Base):
    """124：组织 AI 调用月度预算。

    monthly_limit_cents：本月限额（分）；0 = 未配置（视为不限，记 warning）。
    used_cents：本月已用（分）；period：YYYY-MM。
    """
    __tablename__ = "ai_budgets"
    __table_args__ = (
        UniqueConstraint("org_id", "period", name="uq_budget_org_period"),
        Index("ix_budget_org", "org_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="CASCADE"),
                    nullable=False)
    period = Column(String(7), nullable=False)  # YYYY-MM
    monthly_limit_cents = Column(Integer, nullable=False, default=0,
                                 server_default="0")
    used_cents = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


# ---------- Phase 5：AI 可观测性（131/132） ----------

class AICall(Base):
    """131：AI 调用台账。每一次模型调用一条记录，以 provider 返回的 usage 为准。

    status: success / failed / cached（缓存命中，未真实调用）。
    cost_cents: 按 model_prices 估算的费用（分）。
    """
    __tablename__ = "ai_calls"
    __table_args__ = (
        Index("ix_aicall_task_type", "task_type"),
        Index("ix_aicall_model", "model"),
        Index("ix_aicall_created", "created_at"),
        Index("ix_aicall_task", "task_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    task_id = Column(String(32), nullable=True)  # 关联 ai_tasks.id；同步调用为 None
    task_type = Column(String(32), nullable=False)
    model = Column(String(128), nullable=False)
    prompt_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    completion_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    total_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    cost_cents = Column(Integer, nullable=False, default=0, server_default="0")
    status = Column(String(16), nullable=False, default="success",
                    server_default="success")
    error = Column(Text, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    cached = Column(Boolean, nullable=False, default=False, server_default="0")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class ModelPrice(Base):
    """132：模型价格表。每 1k token 的输入/输出价格，用于成本估算。

    同一 model 可有多个 version（价格变更时新增版本）；估算取最新版本。
    """
    __tablename__ = "model_prices"
    __table_args__ = (
        UniqueConstraint("model", "version", name="uq_modelprice_model_version"),
        Index("ix_modelprice_model", "model"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    model = Column(String(128), nullable=False)
    input_price_per_1k = Column(Float, nullable=False)
    output_price_per_1k = Column(Float, nullable=False)
    currency = Column(String(8), nullable=False, default="CNY",
                      server_default="CNY")
    version = Column(Integer, nullable=False, default=1, server_default="1")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
