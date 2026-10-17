"""S6.1：事实状态机。

状态：
    draft          草稿（新建事实的初始状态）
    pending_review 待审核（已提交人工/规则审核）
    confirmed      已确认（可作为宣传依据：要求 allow_use=true 且有 verified_by）
    rejected       已驳回（需给出原因）
    expired        已过期（expires_at 到期自动转入，或人工标记）
    revoked        已撤销（曾确认后被撤回，需给出原因）

合法流转：
    draft          -> pending_review
    pending_review -> confirmed | rejected
    confirmed      -> expired | revoked
    expired        -> pending_review   （重新核验）
    revoked        -> pending_review   （重新核验）
    rejected       -> draft | pending_review  （修改后重新提交）

兼容：历史数据中的旧值 "pending" 视为 pending_review 的别名
（0005 迁移已把存量 pending 改写为 pending_review，此处做双保险）。

确认/驳回/撤销必须填写 reason（S6 要求：状态变更记录操作者、时间、
原因和证据）。confirmed 还要求 allow_use=true 且 verified_by 非空。
"""
from __future__ import annotations


# ---------------------------------------------------------------- 状态常量

DRAFT = "draft"
PENDING_REVIEW = "pending_review"
CONFIRMED = "confirmed"
REJECTED = "rejected"
EXPIRED = "expired"
REVOKED = "revoked"

# 历史遗留别名（085 时代的 "pending"）
LEGACY_PENDING = "pending"

ALL_STATUSES = (
    DRAFT, PENDING_REVIEW, CONFIRMED, REJECTED, EXPIRED, REVOKED,
)

# AI 可引用的状态（010：还需 allow_use=true，见 guard.py）
USABLE_STATUSES = (CONFIRMED,)


# ---------------------------------------------------------------- 流转表

TRANSITIONS: dict[str, tuple[str, ...]] = {
    DRAFT: (PENDING_REVIEW,),
    PENDING_REVIEW: (CONFIRMED, REJECTED),
    CONFIRMED: (EXPIRED, REVOKED),
    EXPIRED: (PENDING_REVIEW,),
    REVOKED: (PENDING_REVIEW,),
    REJECTED: (DRAFT, PENDING_REVIEW),
}

# 目标状态 -> 是否强制要求 reason
REASON_REQUIRED = {
    CONFIRMED: True,
    REJECTED: True,
    REVOKED: True,
    PENDING_REVIEW: False,
    EXPIRED: False,   # 系统自动过期可无 reason；人工标记建议填
    DRAFT: False,
}


# ---------------------------------------------------------------- 异常

class IllegalFactTransition(ValueError):
    """非法事实状态流转。"""

    def __init__(self, from_status: str, to_status: str):
        self.from_status = from_status
        self.to_status = to_status
        allowed = TRANSITIONS.get(from_status, ())
        super().__init__(
            f"事实状态不允许从 {from_status} 流转到 {to_status}"
            f"（允许：{', '.join(allowed) if allowed else '无'}）")


class FactTransitionRuleError(ValueError):
    """流转规则校验失败（如缺 reason、确认条件不满足）。"""


# ---------------------------------------------------------------- 工具函数

def normalize_status(status: str | None) -> str:
    """归一化状态值：历史 "pending" 视为 pending_review。"""
    if status == LEGACY_PENDING:
        return PENDING_REVIEW
    return status or ""


def validate_transition(from_status: str, to_status: str) -> tuple[str, str]:
    """校验流转合法性。返回归一化后的 (from, to)；非法抛 IllegalFactTransition。

    未知状态值抛 FactTransitionRuleError（先于流转表检查）。
    """
    frm = normalize_status(from_status)
    to = normalize_status(to_status)
    if frm not in ALL_STATUSES:
        raise FactTransitionRuleError(f"未知事实状态: {from_status}")
    if to not in ALL_STATUSES:
        raise FactTransitionRuleError(f"未知事实状态: {to_status}")
    if to not in TRANSITIONS.get(frm, ()):
        raise IllegalFactTransition(frm, to)
    return frm, to


def requires_reason(to_status: str) -> bool:
    """目标状态是否强制要求填写原因。"""
    return REASON_REQUIRED.get(normalize_status(to_status), False)


def check_confirm_requirements(allow_use: bool | None,
                               verified_by: str | None) -> None:
    """confirmed 的硬性条件：allow_use=true 且 verified_by 非空。

    对应 010（事实 grounding）与 S6.1：只有"已确认且允许使用"的事实
    才能作为宣传依据。
    """
    if allow_use is not True:
        raise FactTransitionRuleError(
            "确认事实必须设置 allow_use=true（允许作为宣传依据）")
    if not (verified_by or "").strip():
        raise FactTransitionRuleError(
            "确认事实必须记录 verified_by（确认人）")
