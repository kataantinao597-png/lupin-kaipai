"""087：Repository 层统一事务机制 + 088 乐观锁异常。"""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
from typing import Iterator

from sqlalchemy.orm import Session


class OptimisticLockError(Exception):
    """088：更新时 version 不匹配。API 层映射为 HTTP 409。"""

    def __init__(self, entity: str, entity_id: str,
                 expected: int | None = None, actual: int | None = None):
        self.entity = entity
        self.entity_id = entity_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"{entity}({entity_id}) 版本冲突：期望 version={expected}，"
            f"当前 version={actual}，请刷新后重试"
        )


class EntityNotFound(Exception):
    """实体不存在（或无权访问时按 404 处理，防枚举）。"""

    def __init__(self, entity: str, entity_id: str):
        self.entity = entity
        self.entity_id = entity_id
        super().__init__(f"{entity}({entity_id}) 不存在")


@contextmanager
def transactional(db: Session) -> Iterator[Session]:
    """087：跨表修改的统一事务——要么全成功，要么全回滚。

    若调用方会话已在事务中（例如请求内先做过查询），则用 SAVEPOINT
    嵌套，保证与外层事务语义一致。
    """
    if db.in_transaction():
        with db.begin_nested():
            yield db
    else:
        with db.begin():
            yield db


def transactional_method(fn):
    """087：Repository 方法装饰器，自动包裹 transactional。"""

    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        with transactional(self.db):
            return fn(self, *args, **kwargs)

    return wrapper
