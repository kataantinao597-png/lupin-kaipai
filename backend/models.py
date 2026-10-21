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
    Date,
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
    # S8.3：镜头参考图版权/来源标注（AI 生成参考须标明，不代表真实产品）
    ref_image_copyright = Column(String(256), nullable=True)
    ref_image_source = Column(String(512), nullable=True)
    ref_image_license = Column(String(128), nullable=True)
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
    scope = Column(String(128), nullable=False, default="", server_default="",
                   comment="适用范围，如：仅烟台产地款")
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


class Product(Base):
    """V3.0 B-1：产品档案（跨项目复用的产品资料中心）。

    - 产品信息不再只散落在各 work.data_json，由档案统一维护。
    - code 在组织内唯一，是产品的稳定业务标识；
      facts.product_id 自由字符串可引用 id 或 code（B-2 兼容）。
    - 删除产品时若仍有 facts 引用（id 或 code 任一命中），禁止删除。
    - version 乐观锁（088 同模式），更新必须带 expected_version。
    """
    __tablename__ = "products"
    __table_args__ = (
        Index("ix_product_org", "org_id"),
        UniqueConstraint("org_id", "code", name="uq_product_org_code"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="CASCADE"),
                    nullable=False)
    code = Column(String(64), nullable=False)  # 组织内唯一的产品编号
    name = Column(String(128), nullable=False)
    category = Column(String(64), nullable=False, default="")  # 品类
    origin = Column(String(128), nullable=False, default="")  # 产地
    brand_profile = Column(Text, nullable=False, default="")  # 品牌资料
    brand_tone = Column(String(128), nullable=False, default="")  # 品牌语气
    selling_points = Column(JSON, nullable=False, default=list)  # 卖点列表
    ad_restrictions = Column(Text, nullable=False, default="")  # 宣传限制
    no_expand_claims = Column(Text, nullable=False, default="")  # 禁止擅自扩展的信息
    # 088：乐观锁版本
    version = Column(Integer, nullable=False, default=1, server_default="1")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


class ProductRef(Base):
    """V3.0 B-5：产品档案引用标记（轻量引用表）。

    - 记录哪些作品内容在生成时引用了哪个产品档案的哪个版本。
    - 产品资料变更后，只把引用旧版本的标记为 stale=True，
      绝不静默改写历史文案或已确认分镜（任务书 §4.3 铁律）。
    - 产品删除时级联删除标记（内容本身不受影响）。
    """
    __tablename__ = "product_refs"
    __table_args__ = (
        Index("ix_productref_org", "org_id"),
        Index("ix_productref_product", "product_id"),
        Index("ix_productref_ref", "ref_type", "ref_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="CASCADE"),
                    nullable=False)
    product_id = Column(String(32), ForeignKey("products.id", ondelete="CASCADE"),
                        nullable=False)
    ref_type = Column(String(16), nullable=False, default="work")  # 目前只有 work
    ref_id = Column(String(32), nullable=False)  # work.id
    # 引用时记录的产品版本号 + 快照信息（展示用，不回写）
    product_version = Column(Integer, nullable=False)
    product_code = Column(String(64), nullable=False, default="")
    product_name = Column(String(128), nullable=False, default="")
    stale = Column(Boolean, nullable=False, default=False, server_default="0")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


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
TASK_TYPE_LIVESTREAM = "livestream_script"
TASK_TYPE_LIVESTREAM_REGEN = "livestream_regen_segment"
TASK_TYPE_AGENT_RUN = "agent_run"  # Phase 3：AI 对话式 Agent run
TASK_TYPES = (
    TASK_TYPE_IDEAS, TASK_TYPE_STORYBOARD, TASK_TYPE_REGEN_SHOT,
    TASK_TYPE_RISK, TASK_TYPE_FEASIBILITY, TASK_TYPE_LIVESTREAM,
    TASK_TYPE_LIVESTREAM_REGEN, TASK_TYPE_AGENT_RUN,
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
    # 实际使用的 prompt 版本（141-160：prompt 版本化，改 prompt 必须升版本）
    prompt_version = Column(String(32), nullable=True)
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
        Index("ix_aicall_org", "org_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    task_id = Column(String(32), nullable=True)  # 关联 ai_tasks.id；同步调用为 None
    # P1 修复：归属组织。同步调用没有 ai_tasks 行，只能靠这一列做租户归属，
    # 否则「用量仪表盘/组织配额/用量报表」三个口径都把同步调用算作 0。
    # 可为空：匿名调用（未声称 org_id）确实不属于任何组织。
    org_id = Column(String(32), nullable=True)
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


# ---------------------------------------------------------------------------
# Phase 10（AI 网关扩展）：任务选模型策略 + 模型能力注册表（128/129）
# ---------------------------------------------------------------------------

# 已知任务类型（与 RUNNERS / gateway task_type 对齐）
AI_TASK_TYPES = ("ideas", "storyboard", "regen_shot", "feasibility", "risk_check",
                 "livestream_script", "livestream_regen_segment",
                 "colloquial_rewrite")


class TaskModelPolicy(Base):
    """128：任务选模型策略。task_type → 主模型 + 兜底模型。

    model=None 的调用走此表；调用方显式指定模型时优先用指定的。
    fallback_model 用于 130 故障切换（主模型失败后自动切一次）。
    """
    __tablename__ = "task_model_policy"

    task_type = Column(String(32), primary_key=True)
    model = Column(String(64), nullable=False)
    fallback_model = Column(String(64), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


class ModelCapability(Base):
    """129：模型能力注册表。调用前校验任务需求 vs 模型能力。

    max_tokens=0 表示未知；supports_* 为种子值时以管理后台更新为准。
    """
    __tablename__ = "model_capabilities"

    model = Column(String(64), primary_key=True)
    max_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    supports_vision = Column(Boolean, nullable=False, default=False,
                             server_default="0")
    supports_json = Column(Boolean, nullable=False, default=True,
                           server_default="1")
    cost_tier = Column(String(16), nullable=False, default="standard",
                       server_default="standard")
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


# ---------------------------------------------------------------------------
# Phase 9（安全纵深）：集成 API Key（服务账号）+ 审计日志（9-4/9-5）
# ---------------------------------------------------------------------------

class ApiKey(Base):
    """集成用 API Key（服务账号）。9-4

    只存 key_hash（SHA256），明文仅创建时返回一次。
    scopes 为 JSON 列表，如 ["works:read", "ai:use"]。
    """
    __tablename__ = "api_keys"
    __table_args__ = (
        Index("ix_apikey_org", "org_id"),
        Index("ix_apikey_hash", "key_hash", unique=True),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), ForeignKey("organizations.id",
                                           ondelete="CASCADE"),
                    nullable=False)
    name = Column(String(128), nullable=False)
    key_hash = Column(String(64), nullable=False)      # SHA256 hex
    key_prefix = Column(String(16), nullable=False)    # 明文前 10 字符，便于辨认
    scopes = Column(JSON, nullable=False, default=list)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(String(64), nullable=False)    # 创建者 user_id（逻辑关联）
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class AuditEvent(Base):
    """关键操作审计事件。9-5

    action 如 api_key.create / work.delete / org.member.set_role /
    waiver.create 等；detail_json 存差异摘要，不记敏感内容。
    """
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_org_created", "org_id", "created_at"),
        Index("ix_audit_action", "action"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), nullable=True)
    user_id = Column(String(64), nullable=True)        # 操作者（逻辑关联）
    action = Column(String(64), nullable=False)
    object_type = Column(String(32), nullable=True)
    object_id = Column(String(64), nullable=True)
    detail_json = Column(JSON, nullable=True)
    ip = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


# ---------------------------------------------------------------------------
# Phase 12（企业管理与商业化 S10）：配额 / Webhook / 模板市场 / 计费
# ---------------------------------------------------------------------------


class Quota(Base):
    """S10 配额：组织维度硬限额。

    quota_type: ai_calls_daily（组织每日 AI 调用次数）
                | storage_mb（作品 data_json 总量，MB）
                | members（组织成员数）。
    limit_value <= 0 视为未配置（不限）。
    """
    __tablename__ = "quotas"
    __table_args__ = (
        UniqueConstraint("org_id", "quota_type", name="uq_quota_org_type"),
        Index("ix_quota_org", "org_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="CASCADE"),
                    nullable=False)
    quota_type = Column(String(32), nullable=False)
    limit_value = Column(Integer, nullable=False, default=0, server_default="0")
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


class Webhook(Base):
    """S10 Webhook 订阅：组织级事件通知。

    events_json: JSON 数组，如 ["task.completed", "review.expired"]。
    secret: HMAC-SHA256 签名密钥，只存库中，创建时向调用方返回一次明文。
    """
    __tablename__ = "webhooks"
    __table_args__ = (
        Index("ix_webhook_org", "org_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="CASCADE"),
                    nullable=False)
    url = Column(Text, nullable=False)
    events_json = Column(Text, nullable=False, default="[]")
    secret = Column(String(64), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True, server_default="1")
    created_by = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class WebhookDelivery(Base):
    """S10 Webhook 投递记录：status pending（待投递/重试中）/ sent / dead（死信）。

    失败最多重试 3 次（指数退避），之后进死信，可在管理 API 查看。
    """
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        Index("ix_whdelivery_wh", "webhook_id"),
        Index("ix_whdelivery_status", "status"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    webhook_id = Column(String(32), ForeignKey("webhooks.id", ondelete="CASCADE"),
                        nullable=False)
    event_type = Column(String(64), nullable=False)
    payload_json = Column(Text, nullable=False, default="{}")
    status = Column(String(16), nullable=False, default="pending",
                    server_default="pending")
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    next_retry_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    delivered_at = Column(DateTime(timezone=True), nullable=True)


class Template(Base):
    """S10 模板市场：分镜/创意模板。更新内容时 version+1，旧版本快照进
    template_versions。上架审核流留 TODO（当前 is_public 由管理员直接控制）。"""
    __tablename__ = "templates"
    __table_args__ = (
        Index("ix_template_org", "org_id"),
        Index("ix_template_public", "is_public"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="CASCADE"),
                    nullable=False)
    name = Column(String(128), nullable=False)
    category = Column(String(64), nullable=False, default="", server_default="")
    content_json = Column(Text, nullable=False, default="{}")
    version = Column(Integer, nullable=False, default=1, server_default="1")
    is_public = Column(Boolean, nullable=False, default=False, server_default="0")
    # custom（自建）| official（官方）| shared（他组织共享）
    source = Column(String(32), nullable=False, default="custom",
                    server_default="custom")
    created_by = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


class TemplateVersion(Base):
    """S10 模板版本快照：每次更新内容前存一份。"""
    __tablename__ = "template_versions"
    __table_args__ = (
        Index("ix_tversion_tpl", "template_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    template_id = Column(String(32), ForeignKey("templates.id", ondelete="CASCADE"),
                         nullable=False)
    version = Column(Integer, nullable=False)
    content_json = Column(Text, nullable=False, default="{}")
    created_by = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class PriceConfig(Base):
    """S10 计费价格配置：只增版本、不改历史（对账可追溯）。

    item: ai_token_1k（每千 token）| ai_call（每次调用）
          | storage_gb_month（每 GB·月）| member_seat（每成员席位）。
    注意：seed 数据为 0 占位，上线前必须由运营配置真实价格。
    """
    __tablename__ = "price_configs"
    __table_args__ = (
        UniqueConstraint("item", "version", name="uq_price_item_version"),
        Index("ix_price_item", "item"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    item = Column(String(64), nullable=False)
    unit_price_cents = Column(Integer, nullable=False, default=0,
                              server_default="0")
    currency = Column(String(8), nullable=False, default="CNY",
                      server_default="CNY")
    version = Column(Integer, nullable=False, default=1, server_default="1")
    note = Column(String(256), nullable=False, default="", server_default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class BillingRecord(Base):
    """S10 对账单：period=YYYY-MM；status draft/issued/paid/void。

    usage_json 快照对账依据（用量明细 + 当时价格版本），金额可重算追溯。
    """
    __tablename__ = "billing_records"
    __table_args__ = (
        UniqueConstraint("org_id", "period", name="uq_billing_org_period"),
        Index("ix_billing_org", "org_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="CASCADE"),
                    nullable=False)
    period = Column(String(7), nullable=False)  # YYYY-MM
    usage_json = Column(Text, nullable=False, default="{}")
    amount_cents = Column(Integer, nullable=False, default=0, server_default="0")
    currency = Column(String(8), nullable=False, default="CNY",
                      server_default="CNY")
    status = Column(String(16), nullable=False, default="draft",
                    server_default="draft")
    issued_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


# ---------------------------------------------------------------------------
# S8.1/S8.2：媒体资产（对象存储元数据 + 上传意图 + 处理任务）
# ---------------------------------------------------------------------------

# 资产状态
ASSET_STATUS_UPLOADING = "uploading"  # 已创建意图，等待文件
ASSET_STATUS_READY = "ready"          # 已入库，可用
ASSET_STATUS_FAILED = "failed"        # 校验/入库失败
ASSET_STATUS_DELETED = "deleted"      # 已删除（文件已清）
ASSET_STATUSES = (
    ASSET_STATUS_UPLOADING, ASSET_STATUS_READY,
    ASSET_STATUS_FAILED, ASSET_STATUS_DELETED,
)

# 媒体处理任务状态
MEDIA_TASK_PENDING = "pending"
MEDIA_TASK_RUNNING = "running"
MEDIA_TASK_SUCCEEDED = "succeeded"
MEDIA_TASK_FAILED = "failed"
MEDIA_TASK_STATUSES = (
    MEDIA_TASK_PENDING, MEDIA_TASK_RUNNING,
    MEDIA_TASK_SUCCEEDED, MEDIA_TASK_FAILED,
)


class Asset(Base):
    """S8.1：媒体资产元数据。业务库只存元数据 + 对象 key，文件本身在对象存储。

    key 全局唯一，格式 `{org_id}/{work_id}/{uuid}_{safe_name}`。
    """
    __tablename__ = "assets"
    __table_args__ = (
        Index("ix_asset_org_work", "org_id", "work_id"),
        Index("ix_asset_status", "status"),
        UniqueConstraint("key", name="uq_asset_key"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    # 086 同理：组织删除先显式处理资产，RESTRICT 防误删连带
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="RESTRICT"),
                    nullable=False)
    work_id = Column(String(32), ForeignKey("works.id", ondelete="CASCADE"),
                     nullable=False)
    key = Column(String(512), nullable=False, unique=True)
    filename = Column(String(256), nullable=False, default="")
    mime = Column(String(128), nullable=False, default="")
    size = Column(Integer, nullable=False, default=0)
    sha256 = Column(String(64), nullable=True)
    status = Column(String(16), nullable=False, default=ASSET_STATUS_UPLOADING,
                    server_default=ASSET_STATUS_UPLOADING)
    # 处理任务提取的元数据（宽高/时长/分辨率等），JSON 字符串
    meta_json = Column(Text, nullable=False, default="{}")
    created_by = Column(String(64), nullable=False, default="")  # 逻辑关联 users.id
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


class AssetRef(Base):
    """V3.0 G：跨项目素材引用关系（只记引用，不复制文件）。

    项目 B 引用项目 A 的素材：记一条 (asset_id, dest_work_id)，
    文件实体仍属于项目 A。删引用 ≠ 删实体；仍被引用的资产实体
    DELETE 由 API 层拦截（409/ASSET_HAS_REFS），此处只定义关系。
    """
    __tablename__ = "asset_refs"
    __table_args__ = (
        Index("ix_ref_org", "org_id"),
        Index("ix_ref_asset", "asset_id"),
        Index("ix_ref_dest", "dest_work_id"),
        Index("ix_ref_org_asset", "org_id", "asset_id"),
        UniqueConstraint("org_id", "asset_id", "dest_work_id",
                         name="uq_ref_org_asset_dest"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    # 086 同理：组织删除先显式处理资产，RESTRICT 防误删连带
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="RESTRICT"),
                    nullable=False)
    asset_id = Column(String(32), ForeignKey("assets.id", ondelete="CASCADE"),
                      nullable=False)
    # 来源项目（引用创建时的快照）：来源项目被删时置空，引用关系保留
    source_work_id = Column(String(32), ForeignKey("works.id", ondelete="SET NULL"),
                            nullable=True)
    # 目标项目（被引用的项目）：目标项目被删时引用自动清
    dest_work_id = Column(String(32), ForeignKey("works.id", ondelete="CASCADE"),
                          nullable=False)
    created_by = Column(String(64), nullable=False, default="")  # 逻辑关联 users.id
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class UploadIntent(Base):
    """S8.1：一次性上传授权。后端签发 token，前端凭 token 上传文件。

    token 即主键，一次有效（used=True 后作废），15 分钟过期。
    """
    __tablename__ = "upload_intents"
    __table_args__ = (
        Index("ix_intent_org", "org_id"),
        Index("ix_intent_expires", "expires_at"),
    )

    token = Column(String(64), primary_key=True)
    org_id = Column(String(32), nullable=False)
    work_id = Column(String(32), nullable=False)
    key = Column(String(512), nullable=False)  # 预分配的对象 key
    filename = Column(String(256), nullable=False, default="")
    mime = Column(String(128), nullable=False, default="")  # 声明的 mime（上传时再验魔数）
    expected_size = Column(Integer, nullable=False, default=0)
    expected_sha256 = Column(String(64), nullable=True)
    used = Column(Boolean, nullable=False, default=False, server_default="0")
    created_by = Column(String(64), nullable=False, default="")
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class MediaTask(Base):
    """S8.2：媒体处理任务。上传完成后创建，工作线程提取元数据。

    一个资产一条任务（asset_id 唯一）；meta_json 写回 assets.meta_json。
    """
    __tablename__ = "media_tasks"
    __table_args__ = (
        Index("ix_mediatask_status", "status"),
        UniqueConstraint("asset_id", name="uq_mediatask_asset"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    asset_id = Column(String(32), ForeignKey("assets.id", ondelete="CASCADE"),
                      nullable=False, unique=True)
    status = Column(String(16), nullable=False, default=MEDIA_TASK_PENDING,
                    server_default=MEDIA_TASK_PENDING)
    meta_json = Column(Text, nullable=False, default="{}")
    error = Column(Text, nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


# ---------------------------------------------------------------------------
# V3.0 D：独立文案优化器 —— 文案文档 + 轻量版本表
#
# 审计结论 C：只有 WorkVersion 整包版本，colloquial 单次改写不存历史。
# CopyDoc 是一次文案任务（5 种 kind 之一）；CopyVersion 是该文档的版本链。
#
# 铁律（任务书 §6.2/§6.5）：
# - 用户未确认绝不覆盖原文或已确认版本 → 所有"修改"都是新增版本行，
#   旧行只读；恢复 = 新建版本行（注明来源），不改旧行。
# - 当前有效版本 = 该 doc 下 status="confirmed" 且 version_no 最大的行。
# - AI 生成的版本初始 status="draft"；只有用户显式 confirm 才能转正。
# ---------------------------------------------------------------------------

COPY_KINDS = (
    "optimize",   # 优化用户已有文案
    "generate",   # 从零生成新文案
    "script",     # 生成完整视频脚本
    "platform",   # 生成平台发布文案
    "adapt",      # 按平台调整同一份文案
)

COPY_MODES = (
    "natural",        # 自然口语化
    "premium",        # 品牌高级感
    "shandong",       # 山东本土表达
    "ecommerce",      # 电商转化表达
    "hook",           # 开头吸引力优化
    "concise",        # 结构精简
    "voiceover",      # 旁白口播优化
    "platform_post",  # 发布标题/正文/话题/行动引导优化
    "custom",         # 用户自定义优化要求
)

COPY_STATUS_DRAFT = "draft"          # AI 生成待确认
COPY_STATUS_CONFIRMED = "confirmed"  # 用户已确认（v1 原文建档即确认）

COPY_RISK_UNCHECKED = "unchecked"  # 尚未跑风险检查
COPY_RISK_CHECKED = "checked"      # 规则层（rule_scan + fact grounding）已跑
COPY_RISK_FAILED = "failed"        # 风险检查执行失败

# V3.0 E：多平台内容适配
PLATFORM_IDS = (
    "douyin",       # 抖音
    "kuaishou",     # 快手
    "xiaohongshu",  # 小红书
    "shipinhao",    # 视频号
)

PLATFORM_REVIEW_UNREVIEWED = "unreviewed"  # 未人工审核
PLATFORM_REVIEW_REVIEWED = "reviewed"      # 已人工审核


class PlatformVersion(Base):
    """V3.0 E：平台发布版本（从已确认文案版本派生的独立平台版本）。

    - 每个 (doc_id, platform) 可有多个 revision_no，各版本独立保存人工调整；
      修改一个平台版本绝不触及其他平台版本。
    - copy_version_id 关联源文案版本；源版本删除时 SET NULL，
      source_version_no 快照保留，保证关联可追溯。
    - 首期只做可检查、可复制、可导出的发布内容；不做自动发布。
    """

    __tablename__ = "platform_versions"
    __table_args__ = (
        Index("ix_platver_org", "org_id"),
        Index("ix_platver_doc", "doc_id"),
        Index("ix_platver_doc_platform", "doc_id", "platform"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="CASCADE"),
                    nullable=False)
    doc_id = Column(String(32), ForeignKey("copy_docs.id", ondelete="CASCADE"),
                    nullable=False)
    copy_version_id = Column(String(32),
                             ForeignKey("copy_versions.id", ondelete="SET NULL"),
                             nullable=True)
    source_version_no = Column(Integer, nullable=False, default=0)
    platform = Column(String(32), nullable=False)
    revision_no = Column(Integer, nullable=False, default=1)
    # 发布内容
    title = Column(String(256), nullable=False, default="")
    body = Column(Text, nullable=False, default="")
    hashtags = Column(JSON, nullable=False, default=list)
    cover_text = Column(String(256), nullable=False, default="")
    pinned_comment = Column(Text, nullable=False, default="")
    cta = Column(String(256), nullable=False, default="")
    # 人工审核
    review_status = Column(String(16), nullable=False,
                           default=PLATFORM_REVIEW_UNREVIEWED)
    # AI 元信息与风险（可追溯）
    changes_summary = Column(Text, nullable=False, default="")
    fact_ids = Column(JSON, nullable=False, default=list)
    risk_status = Column(String(16), nullable=False, default=COPY_RISK_UNCHECKED)
    risk_flags = Column(JSON, nullable=False, default=list)
    ai_meta = Column(JSON, nullable=False, default=dict)
    created_by = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


class CopyDoc(Base):
    """V3.0 D：文案文档（一次独立文案任务）。

    kind 区分 5 种工作，输入输出契约不同（见 copyopt.py），
    但共用同一套版本/风险/权限机制。
    """
    __tablename__ = "copy_docs"
    __table_args__ = (
        Index("ix_copydoc_org", "org_id"),
        Index("ix_copydoc_kind", "org_id", "kind"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="CASCADE"),
                    nullable=False)
    title = Column(String(256), nullable=False, default="")
    kind = Column(String(16), nullable=False, default="optimize")
    product_id = Column(String(32), ForeignKey("products.id", ondelete="SET NULL"),
                        nullable=True)  # 可选关联产品档案
    created_by = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


class CopyVersion(Base):
    """V3.0 D：文案版本（轻量版本表，只读追加）。

    - version_no 在 doc 内单调递增；(doc_id, version_no) 唯一。
    - source_text：产出本版本的输入文本（优化/改写类）；generate 类为空。
    - text：本版本正文（用户确认后生效的内容）。
    - structured：平台发布类文案的结构化字段（标题/正文/话题/行动引导），JSON。
    - status：draft（AI 生成待确认）/ confirmed（用户已确认）。
    - 恢复某版本 = 新建 confirmed 行（text 复制目标版本，restored_from 标注），
      绝不修改已有行。
    """
    __tablename__ = "copy_versions"
    __table_args__ = (
        Index("ix_copyver_org", "org_id"),
        Index("ix_copyver_doc", "doc_id"),
        UniqueConstraint("doc_id", "version_no", name="uq_copyver_no"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="CASCADE"),
                    nullable=False)
    doc_id = Column(String(32), ForeignKey("copy_docs.id", ondelete="CASCADE"),
                    nullable=False)
    version_no = Column(Integer, nullable=False)
    mode = Column(String(16), nullable=False, default="natural")
    status = Column(String(16), nullable=False, default=COPY_STATUS_DRAFT)
    # 输入输出
    source_text = Column(Text, nullable=False, default="")
    text = Column(Text, nullable=False, default="")
    structured = Column(JSON, nullable=False, default=dict)  # 平台文案结构化字段
    # 优化目标与约束
    goal = Column(String(256), nullable=False, default="")
    style = Column(String(128), nullable=False, default="")
    audience = Column(String(128), nullable=False, default="")
    length_limit = Column(String(64), nullable=False, default="")
    extra_requirements = Column(Text, nullable=False, default="")
    custom_instruction = Column(Text, nullable=False, default="")
    changes_summary = Column(Text, nullable=False, default="")  # AI 给的修改说明
    # 事实与风险
    fact_ids = Column(JSON, nullable=False, default=list)  # 涉及的产品事实 ID
    risk_status = Column(String(16), nullable=False, default=COPY_RISK_UNCHECKED)
    risk_flags = Column(JSON, nullable=False, default=list)
    risk_checked_at = Column(DateTime(timezone=True), nullable=True)
    # AI 元信息（可追溯，不含密钥）
    ai_meta = Column(JSON, nullable=False, default=dict)
    restored_from = Column(String(32), nullable=True)  # 恢复来源版本 id
    created_by = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


# ---------------------------------------------------------------------------
# V3.0 C：内容策划任务（内容策划工作台）。
#
# 铁律：
# - 只记录"营销目标→候选创意→选中→关联拍摄项目"的策划流程，
#   不重复生成完整的拍摄执行单；分镜/实拍继续走 V2.1 既有流程。
# - 创意确认后关联 work_id；关联只是记录关系，不改写 work 的已有内容
#   （direction 写入走 works 既有更新接口）。
# - status：draft（刚建）/ planned（已生成候选）/ linked（已关联拍摄项目）
#   / archived（归档）。
# ---------------------------------------------------------------------------

PLAN_GOALS = (
    "brand",    # 品牌宣传：品牌识别+产品故事+视觉一致性
    "sales",    # 商品销售：真实购买理由+明确产品信息+行动引导
    "intro",    # 产品介绍：已确认产品特征
    "record",   # 产地记录：真实场景+可核实过程
)

PLAN_STATUS_DRAFT = "draft"
PLAN_STATUS_PLANNED = "planned"
PLAN_STATUS_LINKED = "linked"
PLAN_STATUS_ARCHIVED = "archived"


class ContentTask(Base):
    """V3.0 C：内容策划任务（一次从营销目标到创意方向的策划）。"""
    __tablename__ = "content_tasks"
    __table_args__ = (
        Index("ix_contenttask_org", "org_id"),
        Index("ix_contenttask_goal", "org_id", "content_goal"),
        Index("ix_contenttask_work", "work_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="CASCADE"),
                    nullable=False)
    title = Column(String(256), nullable=False, default="")
    content_goal = Column(String(16), nullable=False, default="brand")
    # 可选关联产品档案（复用 B-4 grounding 机制）
    product_id = Column(String(32), ForeignKey("products.id", ondelete="SET NULL"),
                        nullable=True)
    # 策划输入
    audience = Column(String(256), nullable=False, default="")
    content_purpose = Column(String(256), nullable=False, default="")
    core_message = Column(Text, nullable=False, default="")
    selling_points = Column(JSON, nullable=False, default=list)
    style = Column(String(128), nullable=False, default="")
    target_duration_seconds = Column(Integer, nullable=False, default=30)
    target_platforms = Column(JSON, nullable=False, default=list)
    shoot_conditions = Column(JSON, nullable=False, default=dict)
    ad_restrictions = Column(Text, nullable=False, default="")
    # 生成结果
    candidates = Column(JSON, nullable=False, default=list)
    selected_index = Column(Integer, nullable=True)
    selected_idea = Column(JSON, nullable=True)
    # 关联的 V2.1 拍摄项目（只记录关系）
    work_id = Column(String(32), ForeignKey("works.id", ondelete="SET NULL"),
                     nullable=True)
    status = Column(String(16), nullable=False, default=PLAN_STATUS_DRAFT)
    ai_meta = Column(JSON, nullable=False, default=dict)
    created_by = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


# ---- V3.0 F：发布中心状态常量 ----
PUBLISH_PENDING = "pending"        # 待准备
PUBLISH_REVIEW = "review"         # 待审核
PUBLISH_READY = "ready"           # 待发布
PUBLISH_PUBLISHED = "published"   # 已发布
PUBLISH_REVIEWED = "reviewed"     # 已复盘
PUBLISH_FAILED = "failed"         # 失败
PUBLISH_POSTPONED = "postponed"   # 暂缓
PUBLISH_CANCELLED = "cancelled"   # 取消

PUBLISH_STATUSES = (
    PUBLISH_PENDING, PUBLISH_REVIEW, PUBLISH_READY, PUBLISH_PUBLISHED,
    PUBLISH_REVIEWED, PUBLISH_FAILED, PUBLISH_POSTPONED, PUBLISH_CANCELLED,
)

# 合法状态转换（后端强制；各阶段独立，绝不自动推进）
PUBLISH_TRANSITIONS = {
    PUBLISH_PENDING: (PUBLISH_REVIEW, PUBLISH_CANCELLED),
    PUBLISH_REVIEW: (PUBLISH_READY, PUBLISH_PENDING, PUBLISH_CANCELLED),
    PUBLISH_READY: (PUBLISH_PUBLISHED, PUBLISH_POSTPONED, PUBLISH_CANCELLED),
    PUBLISH_PUBLISHED: (PUBLISH_REVIEWED, PUBLISH_FAILED),
    PUBLISH_REVIEWED: (),
    PUBLISH_FAILED: (PUBLISH_PENDING,),
    PUBLISH_POSTPONED: (PUBLISH_PENDING,),
    PUBLISH_CANCELLED: (),
}

# 发布平台（与多平台适配共用同一套 id）
PUBLISH_PLATFORMS = PLATFORM_IDS


class PublishRecord(Base):
    """V3.0 F：发布中心（一次独立的发布任务）。

    - 关联源拍摄项目 / 文案版本 / 平台版本 / 最终视频与封面文件。
    - 状态机见 PUBLISH_TRANSITIONS；生成文案、完成剪辑、实际发布是不同阶段，
      后端绝不因前一阶段成功自动推进后续状态。
    - 发布前检查（check_result）由后端 run_prepublish_check 生成；
      review → ready 要求所有 blocking 项通过；有未处理重大风险时阻止。
    - 首期只支持复制、下载、手动记录发布结果；不做自动发布接口。
    """
    __tablename__ = "publish_records"
    __table_args__ = (
        Index("ix_pubrec_org", "org_id"),
        Index("ix_pubrec_org_status", "org_id", "status"),
        Index("ix_pubrec_work", "work_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), ForeignKey("organizations.id", ondelete="CASCADE"),
                    nullable=False)
    title = Column(String(256), nullable=False, default="")
    # 关联：源拍摄项目 / 文案版本 / 平台版本 / 最终视频与封面
    work_id = Column(String(32), ForeignKey("works.id", ondelete="SET NULL"),
                     nullable=True)
    copy_version_id = Column(String(32),
                             ForeignKey("copy_versions.id", ondelete="SET NULL"),
                             nullable=True)
    platform_version_id = Column(String(32),
                                 ForeignKey("platform_versions.id",
                                            ondelete="SET NULL"),
                                 nullable=True)
    video_asset_id = Column(String(32), ForeignKey("assets.id", ondelete="SET NULL"),
                            nullable=True)
    cover_asset_id = Column(String(32), ForeignKey("assets.id", ondelete="SET NULL"),
                            nullable=True)
    platform = Column(String(32), nullable=False, default="")
    status = Column(String(16), nullable=False, default=PUBLISH_PENDING)
    # 发布前检查
    check_result = Column(JSON, nullable=False, default=list)
    checked_at = Column(DateTime(timezone=True), nullable=True)
    authorization_confirmed = Column(Boolean, nullable=False, default=False,
                                     server_default="0")
    # 发布
    published_at = Column(DateTime(timezone=True), nullable=True)
    publish_url = Column(String(1024), nullable=False, default="")
    # 发布后数据（阶段 H 效果复盘用；首期手动录入）
    post_metrics = Column(JSON, nullable=False, default=dict)
    notes = Column(Text, nullable=False, default="")
    # V3.0 H：复盘结论（人工撰写）与复盘时间
    review_note = Column(Text, nullable=False, default="")
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now,
                        onupdate=_now)


# ---- V3.0 H：效果复盘 ----

# 指标来源：人工录入 / 平台截图导入（数据哪来必须标清）
METRIC_SOURCE_MANUAL = "manual"
METRIC_SOURCE_SCREENSHOT = "screenshot"
METRIC_SOURCES = (METRIC_SOURCE_MANUAL, METRIC_SOURCE_SCREENSHOT)
METRIC_SOURCE_LABELS = {
    METRIC_SOURCE_MANUAL: "人工录入",
    METRIC_SOURCE_SCREENSHOT: "平台截图导入",
}

# 复盘建议处理状态：待人工处理 / 已采纳 / 已忽略
SUGGESTION_PENDING = "pending"
SUGGESTION_ADOPTED = "adopted"
SUGGESTION_DISMISSED = "dismissed"
SUGGESTION_STATUSES = (SUGGESTION_PENDING, SUGGESTION_ADOPTED,
                       SUGGESTION_DISMISSED)


class EffectMetric(Base):
    """V3.0 H：效果指标（一次录入批次）。

    - 所有数据人工录入，绝不编造；每条必带来源 + 数据起止日期。
    - metrics 为灵活 JSON：{plays, likes, comments, shares, favorites,
      followers_gain, ...}，不同平台字段不同。
    - 支持多次追加；历史即"看板"数据源，无数据时展示空状态。
    """
    __tablename__ = "effect_metrics"
    __table_args__ = (
        Index("ix_em_org", "org_id"),
        Index("ix_em_rec", "publish_record_id"),
        Index("ix_em_org_rec", "org_id", "publish_record_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), ForeignKey("organizations.id",
                                          ondelete="CASCADE"),
                    nullable=False)
    publish_record_id = Column(String(32),
                               ForeignKey("publish_records.id",
                                          ondelete="CASCADE"),
                               nullable=False)
    # 数据来源：manual=人工录入 / screenshot=平台截图导入
    source = Column(String(32), nullable=False, default=METRIC_SOURCE_MANUAL)
    # 数据起止日期（这批指标统计的是哪段时间）
    date_start = Column(Date, nullable=True)
    date_end = Column(Date, nullable=True)
    # 指标值（灵活字段，各平台不同）
    metrics = Column(JSON, nullable=False, default=dict)
    note = Column(Text, nullable=False, default="")
    # 录入时间（何时记的这条）
    recorded_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    created_by = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class ReviewSuggestion(Base):
    """V3.0 H：AI 改进建议。

    - AI 建议只展示、只等人工采纳；绝不自动覆盖已确认的文案、
      已发布版本和任何已确认内容（仅存文本 + 状态）。
    """
    __tablename__ = "review_suggestions"
    __table_args__ = (
        Index("ix_rs_org", "org_id"),
        Index("ix_rs_rec", "publish_record_id"),
        Index("ix_rs_org_rec", "org_id", "publish_record_id"),
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    org_id = Column(String(32), ForeignKey("organizations.id",
                                          ondelete="CASCADE"),
                    nullable=False)
    publish_record_id = Column(String(32),
                               ForeignKey("publish_records.id",
                                          ondelete="CASCADE"),
                               nullable=False)
    suggestion_text = Column(Text, nullable=False, default="")
    # 待人工处理 / 已采纳 / 已忽略
    status = Column(String(16), nullable=False, default=SUGGESTION_PENDING)
    ai_meta = Column(JSON, nullable=False, default=dict)
    created_by = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
