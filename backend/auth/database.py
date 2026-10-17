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


# 094：高频查询索引。create_all 不会给已存在的表补建索引/列，
# 这里做幂等补建，保证存量库也能拿到新结构。
# facts/shots 目前以内嵌 JSON 存于 works.data_json，无独立 ORM 表，
# 暂无可建索引；待 Phase 4 拆表后再补。
def ensure_indexes(engine: Engine) -> None:
    from sqlalchemy import Index, text
    from .. import models as biz_models

    # 092：存量 works 表补 deleted_at 列（SQLite 支持 ADD COLUMN）
    try:
        with engine.begin() as conn:
            cols = {r[1] for r in conn.execute(text("PRAGMA table_info(works)"))}
            if "deleted_at" not in cols and "works" in {
                r[0] for r in conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'"))}:
                conn.execute(text("ALTER TABLE works ADD COLUMN deleted_at DATETIME"))
    except Exception:
        pass  # 非 SQLite 或无 works 表时忽略

    pending = [
        Index("ix_organization_created_by", biz_models.Organization.created_by),
        Index("ix_work_org_deleted", biz_models.Work.org_id,
              biz_models.Work.deleted_at),
        Index("ix_workversion_work", biz_models.WorkVersion.work_id,
              biz_models.WorkVersion.version_no),
    ]
    for ix in pending:
        try:
            ix.create(engine, checkfirst=True)
        except Exception:
            # 索引已存在或后端不支持时忽略，不阻断启动
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
    """FastAPI Depends：每个请求一个会话，结束后关闭。"""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
