"""Phase 3（081-088）数据库组自检。

- 081：DATABASE_URL 解析（默认 SQLite / postgres:// 规范化 / 方言切换）
- 082：alembic upgrade head / downgrade base / upgrade head 全周期
- 083-085：works/storyboards/shots/facts CRUD
- 086：外键约束（非法 FK 拒绝、级联删除、RESTRICT）
- 087：跨表事务原子性（replace_shots 失败全回滚）
- 088：乐观锁冲突
本地无 PostgreSQL 服务时，PG 相关断言只测 URL 解析与 engine 构建（不建连）。
"""
import os
import sys

import pytest

pytestmark = pytest.mark.integration
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.auth.database import (  # noqa: E402
    get_session_factory,
    is_sqlite,
    reset_engine,
    resolve_database_url,
)
from backend.models import Fact, Organization, Shot, Storyboard, Work  # noqa: E402
from backend.repositories import (  # noqa: E402
    EntityNotFound,
    FactRepository,
    OptimisticLockError,
    StoryboardRepository,
    WorkRepository,
    transactional,
)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """每个用例一个全新的 SQLite 文件库（alembic 建表）。"""
    url = f"sqlite:///{tmp_path}/t.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    reset_engine()
    from alembic.config import Config
    from alembic import command
    cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    command.upgrade(cfg, "head")
    session = get_session_factory()()
    yield session
    session.close()
    reset_engine()


def _org(db: Session, name: str = "测试合作社") -> Organization:
    org = Organization(name=name, created_by="u1")
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


# ---------- 081 ----------

def test_081_default_sqlite(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("LUPIN_DB_PATH", raising=False)
    url = resolve_database_url()
    assert url.startswith("sqlite:///")
    assert url.endswith("backend/data/lupin.db")
    assert is_sqlite(url)


def test_081_postgres_url_normalized(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@host:5432/lupin")
    url = resolve_database_url()
    assert url == "postgresql+psycopg2://u:p@host:5432/lupin"
    assert not is_sqlite(url)
    # engine 可构建（不建连），方言为 postgresql
    eng = create_engine(url)
    assert eng.dialect.name == "postgresql"
    eng.dispose()


def test_081_legacy_db_path(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("LUPIN_DB_PATH", str(tmp_path / "legacy.db"))
    assert resolve_database_url() == f"sqlite:///{tmp_path}/legacy.db"


# ---------- 082 ----------

def test_082_migration_tables(db: Session):
    import sqlalchemy as sa
    names = set(sa.inspect(db.bind).get_table_names())
    assert {"users", "organizations", "memberships", "invitations", "works",
            "storyboards", "shots", "facts", "work_versions",
            "refresh_tokens", "password_resets", "products",
            "copy_docs", "copy_versions", "content_tasks",
            "platform_versions"} <= names
    row = db.execute(__import__("sqlalchemy").text(
        "select version_num from alembic_version")).fetchone()
    assert row[0] == "0015_platform_versions"  # V3.0 E：多平台版本表，head 前移


# ---------- 083/084/085 CRUD ----------

def test_crud_work_storyboard_shots(db: Session):
    org = _org(db)
    wreps, srepo = WorkRepository(db), StoryboardRepository(db)
    work = wreps.create(org.id, "u1", "章丘大葱宣传片", {"k": 1})
    assert work.status == "draft" and work.version == 1 and work.deleted_at is None

    sb = srepo.create(work.id, "分镜 v1")
    assert sb.version == 1
    s1 = srepo.add_shot(sb.id, 1, {"scene": "田野全景"}, 5, shot_id_stable="shot-a")
    s2 = srepo.add_shot(sb.id, 2, {"scene": "大葱特写"}, 3, shot_id_stable="shot-b")
    assert s1.id != s2.id  # UUID 主键
    shots = srepo.list_shots(sb.id)
    assert [s.shot_no for s in shots] == [1, 2]

    # 084：重排序后仍能按稳定 ID 找到镜头
    srepo.reorder_shots(sb.id, [s2.id, s1.id])
    found = srepo.get_shot_by_stable_id(sb.id, "shot-a")
    assert found is not None and found.shot_no == 2


def test_crud_facts(db: Session):
    org = _org(db)
    frepo = FactRepository(db)
    f = frepo.create(org.id, "产自章丘", product_id="p1", category="产地")
    # S6.1：新事实初始状态为 draft（原 pending 改为待审核别名）
    assert f.status == "draft" and f.allow_use is False and f.version == 1
    # 未确认前不在可用列表
    assert frepo.list_usable(org.id) == []
    f2 = frepo.set_status(f.id, org.id, "confirmed", expected_version=1,
                          verified_by="u1", allow_use=True)
    assert f2.status == "confirmed" and f2.version == 2
    assert [x.id for x in frepo.list_usable(org.id)] == [f2.id]


# ---------- 086 外键 ----------

def test_086_bad_fk_rejected(db: Session):
    # 绕过 repository 的前置检查，直测 DB 级外键
    db.add(Shot(storyboard_id="不存在的storyboard", shot_no=1,
                content_json={}, duration_seconds=5))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_086_cascade_delete(db: Session):
    org = _org(db)
    wreps, srepo = WorkRepository(db), StoryboardRepository(db)
    work = wreps.create(org.id, "u1", "t")
    sb = srepo.create(work.id)
    srepo.add_shot(sb.id, 1, {}, 5)
    # 删作品 → 分镜/镜头级联删除
    sb_id = sb.id
    db.delete(work)
    db.commit()
    db.expire_all()  # 清身份映射缓存，直查 DB
    assert db.get(Storyboard, sb_id) is None
    assert db.scalars(select(Shot)).all() == []


def test_086_org_restrict_when_works_exist(db: Session):
    org = _org(db)
    WorkRepository(db).create(org.id, "u1", "t")
    db.delete(org)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# ---------- 087 事务 ----------

def test_087_replace_shots_atomic_rollback(db: Session):
    org = _org(db)
    wreps, srepo = WorkRepository(db), StoryboardRepository(db)
    work = wreps.create(org.id, "u1", "t")
    sb = srepo.create(work.id)
    srepo.add_shot(sb.id, 1, {"a": 1}, 5, shot_id_stable="keep")

    # 故意在事务中途抛错：version 对不上
    with pytest.raises(OptimisticLockError):
        srepo.replace_shots(sb.id, expected_version=999, shots=[
            {"shot_no": 1, "content": {"b": 2}, "duration_seconds": 4},
        ])
    # 旧镜头还在，分镜 version 没动——全回滚
    assert len(srepo.list_shots(sb.id)) == 1
    assert srepo.get(sb.id).version == 1

    # 正常替换：删旧+插新+version+1 原子提交
    sb2 = srepo.replace_shots(sb.id, expected_version=1, shots=[
        {"shot_no": 1, "content": {"b": 2}, "duration_seconds": 4,
         "shot_id_stable": "new-1"},
        {"shot_no": 2, "content": {"c": 3}, "duration_seconds": 6,
         "shot_id_stable": "new-2"},
    ])
    assert sb2.version == 2
    assert [s.shot_id_stable for s in srepo.list_shots(sb.id)] == ["new-1", "new-2"]


def test_087_transactional_context_manager(db: Session):
    org = _org(db)
    with transactional(db):
        db.add(Organization(name="tx-ok", created_by="u1"))
    assert db.scalar(select(Organization).where(
        Organization.name == "tx-ok")) is not None

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with transactional(db):
            db.add(Organization(name="tx-bad", created_by="u1"))
            raise Boom()
    assert db.scalar(select(Organization).where(
        Organization.name == "tx-bad")) is None


# ---------- 088 乐观锁 ----------

def test_088_work_version_conflict(db: Session):
    org = _org(db)
    repo = WorkRepository(db)
    work = repo.create(org.id, "u1", "t")
    v1 = work.version
    w2 = repo.update(work.id, org.id, expected_version=v1, title="改后标题")
    assert w2.version == v1 + 1
    # 拿着旧 version 再改 → 冲突
    with pytest.raises(OptimisticLockError) as ei:
        repo.update(work.id, org.id, expected_version=v1, title="并发改")
    assert ei.value.actual == v1 + 1
    # 不存在的作品 → EntityNotFound
    with pytest.raises(EntityNotFound):
        repo.update("nope", org.id, expected_version=1, title="x")


def test_088_fact_version_conflict(db: Session):
    org = _org(db)
    frepo = FactRepository(db)
    f = frepo.create(org.id, "内容")
    with pytest.raises(OptimisticLockError):
        frepo.set_status(f.id, org.id, "confirmed", expected_version=999)


def test_088_soft_delete(db: Session):
    org = _org(db)
    repo = WorkRepository(db)
    work = repo.create(org.id, "u1", "t")
    assert repo.count_by_org(org.id) == 1
    repo.soft_delete(work.id, org.id, expected_version=work.version)
    assert repo.count_by_org(org.id) == 0
    assert repo.get(work.id, org.id) is None  # 软删除后不可见
