"""036：AI 任务状态机。

pending → running → succeeded / failed / cancelled
pending → cancelled（排队中直接取消）

终态（succeeded/failed/cancelled）不允许再流转。
"""
from __future__ import annotations

from ..models import (
    TASK_STATUSES,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
    TASK_STATUS_SUCCEEDED,
    TASK_TRANSITIONS,
)

__all__ = [
    "IllegalTaskTransition",
    "validate_task_transition",
    "is_terminal",
    "TERMINAL_STATUSES",
]

TERMINAL_STATUSES = (
    TASK_STATUS_SUCCEEDED, TASK_STATUS_FAILED, TASK_STATUS_CANCELLED,
)


class IllegalTaskTransition(Exception):
    """036：非法任务状态转换。"""

    def __init__(self, task_id: str, from_status: str, to_status: str):
        self.task_id = task_id
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"AITask({task_id}) 非法状态转换：{from_status} -> {to_status}"
        )


def validate_task_transition(task_id: str, from_status: str,
                             to_status: str) -> None:
    """036：校验状态转换合法性。非法抛 IllegalTaskTransition。

    未知状态值也视为非法（防脏数据）。
    """
    if from_status not in TASK_STATUSES:
        raise IllegalTaskTransition(task_id, from_status, to_status)
    if to_status not in TASK_STATUSES:
        raise IllegalTaskTransition(task_id, from_status, to_status)
    allowed = TASK_TRANSITIONS.get(from_status, ())
    if to_status not in allowed:
        raise IllegalTaskTransition(task_id, from_status, to_status)


def is_terminal(status: str) -> bool:
    """是否为终态。"""
    return status in TERMINAL_STATUSES
