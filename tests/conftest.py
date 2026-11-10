"""O-12 测试护栏：全量回归绝不碰业务库。

背景：`backend/auth/database.py:30-47`（`resolve_database_url`）在
`DATABASE_URL` 未设置时默认落到业务库 `backend/data/lupin.db`，且
`LUPIN_DB_PATH` 旧变量在 `DATABASE_URL` 未设置时同样生效；此前全量回归
必须手动指定 scratch 库，漏设即污染业务库并产生 `ai_calls` 噪音行。

本护栏在 pytest 会话开始时（`pytest_sessionstart`）按与 `database.py`
完全相同的优先级解析出生效库地址，出现以下任一情况直接中止整个回归，
而不是静默写脏库：

- `DATABASE_URL` 未设置/为空，且 `LUPIN_DB_PATH` 未设置/为空
  → 生效地址就是默认业务库；
- 生效地址是 SQLite，且解析后的文件路径就是业务库
  `backend/data/lupin.db`（覆盖绝对路径、相对路径、`sqlite:///` 三斜杠
  与 `sqlite:////` 四斜杠、`?mode=` 等查询串、经符号链接的等价路径）；
- `LUPIN_DB_PATH` 指向业务库（仅 `DATABASE_URL` 未设置时生效，与
  `database.py` 的回退逻辑一致）；
- 生效地址不是 SQLite（例如生产 PostgreSQL）：测试只允许 SQLite
  scratch 库，其他一律拦截。

注意：本护栏只看会话启动时的环境变量，不在每个用例里重复检查——
`tests/test_db_phase3.py::test_081_default_sqlite` 会在用例内主动删掉
`DATABASE_URL`/`LUPIN_DB_PATH` 来验证默认解析，用例级拦截会把它误杀。

正确跑法（scratch 库一律放家目录，不要放 /tmp，/tmp 是 512M tmpfs）：

    mkdir -p ~/workspace/.pytest-tmp
    DATABASE_URL="sqlite:///$HOME/workspace/.pytest-tmp/lupin_test.db" \\
        ./venv/bin/python -m pytest tests/ -q -p no:cacheprovider

各测试文件自己的 fixture 会再用 `tmp_path` 建独立 scratch 库覆盖，
这里的环境变量只是兜底：保证没有任何一个用例能落到默认业务库。
"""
import os

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUSINESS_DB_PATH = os.path.realpath(
    os.path.join(_REPO_ROOT, "backend", "data", "lupin.db"))

_HINT = (
    "请设置一个 SQLite scratch 库再跑回归（放家目录，不要放 /tmp）：\n"
    "    DATABASE_URL=\"sqlite:///$HOME/workspace/.pytest-tmp/lupin_test.db\" "
    "./venv/bin/python -m pytest tests/ -q -p no:cacheprovider"
)


def _resolve_effective_url() -> str:
    """与 backend/auth/database.py::resolve_database_url 同优先级解析。

    DATABASE_URL（非空）> LUPIN_DB_PATH（仅 DATABASE_URL 未设置时）
    > 默认业务库。只做解析，不建引擎、不触库。
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    legacy = os.environ.get("LUPIN_DB_PATH", "").strip()
    path = legacy or os.path.join(_REPO_ROOT, "backend", "data", "lupin.db")
    return "sqlite:///%s" % path


def _sqlite_file_realpath(url: str):
    """SQLite URL → 解析后的文件绝对路径；:memory: 返回 None（内存库安全）。

    相对路径按 SQLAlchemy 的口径相对当前工作目录解析；返回 None 表示
    不是文件型 SQLite URL（由调用方另行处理）。
    """
    if not url.startswith("sqlite:///"):
        return None
    rest = url[len("sqlite:///"):]
    rest = rest.split("?", 1)[0].split("#", 1)[0]
    if rest in ("", ":memory:"):
        return None
    if rest.startswith("/"):
        path = rest
    else:
        path = os.path.join(os.getcwd(), rest)
    return os.path.realpath(os.path.normpath(path))


def _check_database_url() -> None:
    """会话启动检查：不满足条件时直接中止回归。"""
    url = _resolve_effective_url()
    if not url.startswith("sqlite"):
        raise pytest.UsageError(
            "O-12 fail-fast：DATABASE_URL 不是 SQLite scratch 库（当前为 %r），"
            "测试只允许 SQLite scratch 库。\n%s" % (url, _HINT))
    real = _sqlite_file_realpath(url)
    if real is None:
        if url == "sqlite:///:memory:":
            return  # 内存库：不落盘，安全
        raise pytest.UsageError(
            "O-12 fail-fast：无法解析出生效 SQLite 文件路径（DATABASE_URL=%r），"
            "不敢放行。\n%s" % (url, _HINT))
    if real == BUSINESS_DB_PATH:
        raise pytest.UsageError(
            "O-12 fail-fast：生效数据库就是业务库 %s，"
            "全量回归已中止，未执行任何用例。\n%s" % (BUSINESS_DB_PATH, _HINT))
    # 生效库是业务库以外的 SQLite 文件（scratch 库）：放行。


def pytest_sessionstart(session):
    _check_database_url()
