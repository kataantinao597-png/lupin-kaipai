"""数据库引擎与会话（Phase 3：PostgreSQL + SQLite 双支持）。

- 081：通过 DATABASE_URL 环境变量选择数据库。
  - 未设置时默认本地 SQLite：sqlite:///<项目>/backend/data/lupin.db（保持本地可跑）
  - 生产设置如：postgresql://user:pass@host:5432/lupin
    （`postgres://` 前缀会自动规范化为 `postgresql://`）
  - 兼容旧变量 LUPIN_DB_PATH（仅当 DATABASE_URL 未设置时生效）
- SQLite 下自动开启 PRAGMA foreign_keys=ON，保证外键约束生效（086）。
- LUPIN_DB_AUTO_CREATE=0 时跳过 create_all（生产走 `alembic upgrade head`）；
  默认为 1，保持本地开发开箱即用。
"""
from __future__ import annotations

import os
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DEFAULT_SQLITE_PATH = os.path.join(DATA_DIR, "lupin.db")


def resolve_database_url() -> str:
    """081：解析数据库连接串。"""
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        # SQLAlchemy 2.x 默认 postgresql:// 走 psycopg 驱动；
        # 本项目依赖是 psycopg2-binary，统一规范化为 psycopg2 驱动，
        # 避免生产因缺驱动启动失败。
        if url.startswith("postgres://"):
            url = "postgresql+psycopg2://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg2://" + url[len("postgresql://"):]
        return url
    legacy = os.environ.get("LUPIN_DB_PATH", "").strip()
    path = legacy or DEFAULT_SQLITE_PATH
    return f"sqlite:///{path}"


def is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _engine_kwargs(url: str) -> dict:
    # 095：连接池管理——pool_size=10、max_overflow=20、
    # pool_timeout=30、pool_recycle=3600。SQLite 文件库同样使用
    # QueuePool，统一配置；PG 额外开启 pool_pre_ping 探测断连。
    kwargs: dict = {
        "pool_size": 10,
        "max_overflow": 20,
        "pool_timeout": 30,
        "pool_recycle": 3600,
    }
    if is_sqlite(url):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_pre_ping"] = True
    return kwargs


# 094：高频查询索引兜底。alembic 迁移（0002）是主路径；
# 这里保证走 create_all 的本地开发库也能拿到索引（checkfirst 幂等）。
# 注：shots(storyboard_id) 的 ix_shot_storyboard 已在模型中定义，
# create_all 会自动建出，无需在此重复。
def ensure_indexes(engine: Engine) -> None:
    from sqlalchemy import Index, text
    from .. import models as biz_models

    pending = [
        Index("ix_organization_created_by", biz_models.Organization.created_by),
        Index("ix_work_org_status", biz_models.Work.org_id,
              biz_models.Work.status),
        Index("ix_work_org_deleted", biz_models.Work.org_id,
              biz_models.Work.deleted_at),
        Index("ix_fact_org_status", biz_models.Fact.org_id,
              biz_models.Fact.status),
        Index("ix_workversion_work", biz_models.WorkVersion.work_id),
        # 2026-10-20：台账按组织查询（用量仪表盘/配额/报表）
        Index("ix_aicall_org", biz_models.AICall.org_id),
    ]
    for ix in pending:
        try:
            ix.create(engine, checkfirst=True)
        except Exception:
            # 索引已存在或后端不支持时忽略，不阻断启动
            pass


def ensure_columns(engine: Engine) -> None:
    """存量库补列（幂等）。

    `create_all(checkfirst=True)` 只补建缺失的**表**，不会给已存在的表加列。
    本项目的库历史上是 create_all 建的（alembic 版本戳落后），所以模型新增
    列后必须在启动时显式 ALTER，否则该列在解析 SQL 时就会报
    `no such column`，台账写入会被静默丢弃。

    只允许追加可空列 / 带默认值的列，不做任何改列或删列。
    """
    from sqlalchemy import inspect, text

    pending = [
        # 2026-10-20 P1：AI 调用台账的组织归属（同步调用没有 ai_tasks 行）
        ("ai_calls", "org_id", "VARCHAR(32)"),
    ]
    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names())
    except Exception:
        return
    for table, col, ddl_type in pending:
        if table not in tables:
            continue
        try:
            existing = {c["name"] for c in insp.get_columns(table)}
            if col in existing:
                continue
            with engine.begin() as conn:
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN {col} {ddl_type}"))
        except Exception:
            # 并发启动/权限不足时不阻断启动；下一轮启动会再试
            pass


def _apply_sqlite_pragmas(dbapi_conn, _conn_record) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = resolve_database_url()
        if is_sqlite(url):
            os.makedirs(DATA_DIR, exist_ok=True)
        _engine = create_engine(url, **_engine_kwargs(url))
        if is_sqlite(url):
            event.listen(_engine, "connect", _apply_sqlite_pragmas)
        # 生产环境（LUPIN_DB_AUTO_CREATE=0）由 alembic upgrade head 建表，
        # 避免 create_all 与迁移版本表打架；本地开发默认保持开箱即用。
        if os.environ.get("LUPIN_DB_AUTO_CREATE", "1") != "0":
            Base.metadata.create_all(_engine)
        ensure_columns(_engine)  # 2026-10-20：存量库补新增列（幂等）
        ensure_indexes(_engine)  # 094：存量库补建高频查询索引
    return _engine


def reset_engine() -> None:
    """仅测试用：丢弃缓存的引擎/会话工厂，让环境变量重新生效。"""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), class_=Session, expire_on_commit=False)
    return _SessionLocal


def get_db() -> Iterator[Session]:
    """FastAPI Depends：每个请求一个会话，结束后关闭。

    S9.3：SQLAlchemy 异常冒泡时记一条 DB 错误指标。
    """
    session = get_session_factory()()
    try:
        yield session
    except Exception:
        try:
            from ..core import metrics as _metrics
            _metrics.record_db(False)
        except Exception:
            pass
        raise
    else:
        try:
            from ..core import metrics as _metrics
            _metrics.record_db(True)
        except Exception:
            pass
    finally:
        session.close()
