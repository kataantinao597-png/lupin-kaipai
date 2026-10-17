"""Alembic 环境（082）。

- 数据库地址统一走 backend.auth.database.resolve_database_url()，
  即 DATABASE_URL 环境变量（默认本地 SQLite，生产配 postgresql://）。
- SQLite 下同样开启 PRAGMA foreign_keys=ON，保证迁移后外键生效。
"""
from __future__ import annotations

import os
import sys

# 让 `import backend...` 可用（从项目根目录运行 alembic 时）
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from alembic import context
from sqlalchemy import engine_from_config, pool

from backend.auth.database import is_sqlite, resolve_database_url
from backend.auth.models import Base  # noqa: F401

# 注册全部业务模型到同一 metadata
import backend.models  # noqa: F401,E402

config = context.config
# 082：ini 里的占位地址一律被 DATABASE_URL 覆盖
config.set_main_option("sqlalchemy.url", resolve_database_url())

target_metadata = Base.metadata


def _sqlite_pragmas(dbapi_conn, _conn_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from sqlalchemy import event

    connect_args = {}
    if is_sqlite(config.get_main_option("sqlalchemy.url")):
        connect_args = {"check_same_thread": False}
    engine = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )
    if is_sqlite(config.get_main_option("sqlalchemy.url")):
        event.listen(engine, "connect", _sqlite_pragmas)

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
