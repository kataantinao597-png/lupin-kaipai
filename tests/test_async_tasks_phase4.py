"""Phase 4 异步任务组自检（032-038）。

- 032：事务边界——分镜更新 / 镜头锁定 / 审核状态
- 033：领域事件 EventBus
- 034/035/037：任务创建 → 查询 → 完成全流程
- 036：非法状态转换断言
- 038：取消（pending 直接取消 / running 协作式取消）

不用真实 AI：runner 用可控的假函数；完整 API 链路用 FastAPI TestClient
（若缺依赖则跳过）。
"""
import os
import sys
import time

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.auth.database import (  # noqa: E402
    get_session_factory,
    reset_engine,
)
from backend.models import Organization  # noqa: E402
from backend.repositories import (  # noqa: E402
    EntityNotFound,
    ReviewRepository,
    StoryboardRepository,
    validate_review_transition,
    IllegalReviewTransition,
)
from backend.ai_tasks.repository import AITaskRepository  # noqa: E402
from backend.ai_tasks.state_machine import (  # noqa: E402
    IllegalTaskTransition,
    is_terminal,
    validate_task_transition,
)
from backend.ai_tasks.queue import TaskQueue, get_executor  # noqa: E402
from backend.core.events import (  # noqa: E402
    DomainEvent,
    FactChanged,
    MemoryEventBus,
    ReviewInvalidated,
    StoryboardUpdated,
    get_event_bus,
    register_default_subscribers,
    reset_event_bus,
)


@pytest.fixture()
def db(tmp_path, monkeypatch):
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


def _org(db, name="测试合作社"):
    org = Organization(name=name, created_by="u1")
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _work(db, org_id, title="测试作品"):
    from backend.models import Work
    w = Work(org_id=org_id, owner_id="u1", title=title)
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


# ================= 032：事务边界 =================

class TestTransactionBoundaries:
    def test_storyboard_update_atomic(self, db):
        """032：分镜更新——replace_shots 失败时全回滚。"""
        org = _org(db)
        work = _work(db, org.id)
        repo = StoryboardRepository(db)
        sb = repo.create(work.id, "分镜1")
        repo.add_shot(sb.id, 1, {"text": "旧镜头"}, 5, "stable-1")
        # 非法 version → 抛错，旧镜头必须还在
        with pytest.raises(Exception):
            repo.replace_shots(sb.id, expected_version=999,
                               shots=[{"shot_no": 1, "content": {"text": "新"}}])
        shots = repo.list_shots(sb.id)
        assert len(shots) == 1
        assert shots[0].content_json == {"text": "旧镜头"}

    def test_shot_lock_transactional(self, db):
        """032：锁定状态——单个锁定事务提交。"""
        org = _org(db)
        work = _work(db, org.id)
        repo = StoryboardRepository(db)
        sb = repo.create(work.id)
        s1 = repo.add_shot(sb.id, 1, {}, 5, "s1")
        s2 = repo.add_shot(sb.id, 2, {}, 5, "s2")
        repo.set_shot_lock(sb.id, s1.id, True)
        assert repo.list_locked_shot_nos(sb.id) == [1]
        # 解锁
        repo.set_shot_lock(sb.id, s1.id, False)
        assert repo.list_locked_shot_nos(sb.id) == []

    def test_shot_lock_batch_rollback(self, db):
        """032：批量锁定——任一非法则整个批次回滚。"""
        org = _org(db)
        work = _work(db, org.id)
        repo = StoryboardRepository(db)
        sb = repo.create(work.id)
        s1 = repo.add_shot(sb.id, 1, {}, 5, "s1")
        with pytest.raises(EntityNotFound):
            repo.set_shots_lock(sb.id, [s1.id, "不存在的id"], True)
        # s1 的锁定位必须回滚（仍为 False）
        assert repo.list_locked_shot_nos(sb.id) == []

    def test_shot_lock_wrong_storyboard(self, db):
        """032：跨分镜的 shot_id 拒绝（防串改）。"""
        org = _org(db)
        work = _work(db, org.id)
        repo = StoryboardRepository(db)
        sb1 = repo.create(work.id)
        sb2 = repo.create(work.id)
        s1 = repo.add_shot(sb1.id, 1, {}, 5, "s1")
        with pytest.raises(EntityNotFound):
            repo.set_shot_lock(sb2.id, s1.id, True)

    def test_review_lifecycle(self, db):
        """032：审核状态——创建 → 流转 → 非法流转拒绝。"""
        org = _org(db)
        work = _work(db, org.id)
        repo = ReviewRepository(db)
        r = repo.create(org.id, work.id, content_hash="hash1")
        assert r.status == "pending"
        r = repo.transition(r.id, "in_review", expected_version=1,
                            reviewer_id="u2")
        assert r.status == "in_review"
        assert r.version == 2
        # 非法流转：in_review 不能直接回 pending
        with pytest.raises(IllegalReviewTransition):
            repo.transition(r.id, "pending", expected_version=2)
        # 乐观锁冲突
        from backend.repositories.base import OptimisticLockError
        with pytest.raises(OptimisticLockError):
            repo.transition(r.id, "approved", expected_version=1)
        # 合法：in_review → approved
        r = repo.transition(r.id, "approved", expected_version=2,
                            reviewer_id="u2",
                            decisions={"r1": "modified"})
        assert r.status == "approved"
        assert r.decisions_json == {"r1": "modified"}

    def test_review_expire_stale(self, db):
        """032/203：内容变更后旧审核自动失效（事务内）。"""
        org = _org(db)
        work = _work(db, org.id)
        repo = ReviewRepository(db)
        r1 = repo.create(org.id, work.id, content_hash="old-hash")
        repo.transition(r1.id, "in_review", expected_version=1)
        repo.transition(r1.id, "approved", expected_version=2)
        # 内容变了 → 旧审核失效
        n = repo.expire_stale(work.id, "new-hash")
        assert n == 1
        r1 = repo.get(r1.id)
        assert r1.status == "expired"
        # 相同哈希不再重复失效
        assert repo.expire_stale(work.id, "new-hash") == 0

    def test_validate_review_transition_fn(self):
        validate_review_transition("pending", "in_review")
        with pytest.raises(IllegalReviewTransition):
            validate_review_transition("approved", "pending")


# ================= 033：领域事件 =================

class TestEventBus:
    def test_publish_subscribe(self):
        bus = MemoryEventBus()
        received = []
        bus.subscribe(StoryboardUpdated, received.append)
        bus.publish(StoryboardUpdated(work_id="w1", storyboard_id="sb1"))
        assert len(received) == 1
        assert received[0].work_id == "w1"

    def test_base_class_subscription(self):
        """基类订阅收到所有事件。"""
        bus = MemoryEventBus()
        received = []
        bus.subscribe(DomainEvent, received.append)
        bus.publish(FactChanged(org_id="o1", fact_id="f1"))
        bus.publish(StoryboardUpdated())
        assert len(received) == 2

    def test_handler_exception_isolated(self):
        """异常隔离：一个 handler 抛错不影响其他。"""
        bus = MemoryEventBus()
        received = []

        def bad(event):
            raise RuntimeError("boom")

        bus.subscribe(StoryboardUpdated, bad)
        bus.subscribe(StoryboardUpdated, received.append)
        bus.publish(StoryboardUpdated())  # 不应抛
        assert len(received) == 1

    def test_unsubscribe(self):
        bus = MemoryEventBus()
        received = []
        bus.subscribe(StoryboardUpdated, received.append)
        bus.unsubscribe(StoryboardUpdated, received.append)
        bus.publish(StoryboardUpdated())
        assert received == []

    def test_storyboard_updated_expires_reviews(self, db):
        """033：分镜修改事件 → 默认订阅者自动失效旧审核。"""
        reset_event_bus()
        bus = get_event_bus()
        org = _org(db)
        work = _work(db, org.id)
        repo = ReviewRepository(db)
        r = repo.create(org.id, work.id, content_hash="hash-old")
        repo.transition(r.id, "in_review", expected_version=1)
        repo.transition(r.id, "approved", expected_version=2)

        invalidated = []
        bus.subscribe(ReviewInvalidated, invalidated.append)
        register_default_subscribers(
            bus, db_session_factory=get_session_factory())
        bus.publish(StoryboardUpdated(
            work_id=work.id, storyboard_id="sb1",
            content_hash="hash-new", actor_id="u1"))
        # 审核已失效
        db.expire_all()
        r = repo.get(r.id)
        assert r.status == "expired"
        # 发布了失效事件
        assert len(invalidated) == 1
        assert invalidated[0].work_id == work.id
        reset_event_bus()


# ================= 036：任务状态机 =================

class TestTaskStateMachine:
    def test_legal_transitions(self):
        validate_task_transition("t1", "pending", "running")
        validate_task_transition("t1", "pending", "cancelled")
        validate_task_transition("t1", "running", "succeeded")
        validate_task_transition("t1", "running", "failed")
        validate_task_transition("t1", "running", "cancelled")

    def test_illegal_transitions(self):
        # 终态不能再转
        for terminal in ("succeeded", "failed", "cancelled"):
            for target in ("pending", "running", "succeeded", "failed",
                           "cancelled"):
                with pytest.raises(IllegalTaskTransition):
                    validate_task_transition("t1", terminal, target)
        # pending 不能直接成功/失败
        with pytest.raises(IllegalTaskTransition):
            validate_task_transition("t1", "pending", "succeeded")
        with pytest.raises(IllegalTaskTransition):
            validate_task_transition("t1", "pending", "failed")
        # running 不能回 pending
        with pytest.raises(IllegalTaskTransition):
            validate_task_transition("t1", "running", "pending")
        # 未知状态
        with pytest.raises(IllegalTaskTransition):
            validate_task_transition("t1", "bogus", "running")

    def test_is_terminal(self):
        assert is_terminal("succeeded")
        assert is_terminal("failed")
        assert is_terminal("cancelled")
        assert not is_terminal("pending")
        assert not is_terminal("running")


# ================= 034/035/037/038：任务全流程 =================

def _fake_runner(result: dict, delay: float = 0.2):
    """可控假 runner：模拟 AI 调用。"""
    def run(task_id, is_cancelled):
        steps = 5
        for _ in range(steps):
            if is_cancelled():
                from backend.ai_tasks.queue import _TaskCancelled
                raise _TaskCancelled()
            time.sleep(delay / steps)
        return result
    return run


class TestTaskLifecycle:
    def test_create_query_complete(self, db):
        """034/035/037：创建 → 查询 → 完成全流程断言。"""
        org = _org(db)
        repo = AITaskRepository(db)
        task = repo.create(org.id, "ideas", {"product": {"a": 1}},
                           created_by="u1")
        assert task.status == "pending"
        assert task.progress == 0

        queue = TaskQueue(get_session_factory())
        queue.submit(task.id, _fake_runner({"ideas": [1, 2, 3]}))

        # 轮询等完成（最多 10 秒）
        deadline = time.time() + 10
        final = None
        while time.time() < deadline:
            db.expire_all()
            t = repo.get(task.id)
            if t.status in ("succeeded", "failed", "cancelled"):
                final = t
                break
            time.sleep(0.2)
        assert final is not None, "任务未在时限内完成"
        assert final.status == "succeeded"
        assert final.result_json == {"ideas": [1, 2, 3]}
        assert final.progress == 100
        assert final.started_at is not None
        assert final.finished_at is not None

    def test_cancel_pending(self, db):
        """038：pending 任务直接取消。"""
        org = _org(db)
        repo = AITaskRepository(db)
        task = repo.create(org.id, "ideas", {})
        # 不 submit，直接走队列取消（模拟排队中）
        queue = TaskQueue(get_session_factory())
        # 先打取消标记再 submit，runner 启动前会检查到
        repo.request_cancel(task.id)
        queue.submit(task.id, _fake_runner({"x": 1}))
        time.sleep(1.0)
        db.expire_all()
        t = repo.get(task.id)
        assert t.status == "cancelled"
        assert t.result_json is None  # 取消的不写结果

    def test_cancel_running(self, db):
        """038：running 任务协作式取消。"""
        org = _org(db)
        repo = AITaskRepository(db)
        task = repo.create(org.id, "ideas", {})
        queue = TaskQueue(get_session_factory())
        queue.submit(task.id, _fake_runner({"x": 1}, delay=3.0))
        # 等它跑起来
        deadline = time.time() + 10
        while time.time() < deadline:
            db.expire_all()
            if repo.get(task.id).status == "running":
                break
            time.sleep(0.2)
        db.expire_all()
        assert repo.get(task.id).status == "running"
        # 请求取消
        status = queue.cancel(task.id)
        assert status in ("running", "cancelled")
        # 等协作式取消生效
        deadline = time.time() + 10
        while time.time() < deadline:
            db.expire_all()
            if repo.get(task.id).status == "cancelled":
                break
            time.sleep(0.2)
        db.expire_all()
        assert repo.get(task.id).status == "cancelled"

    def test_cancel_terminal_idempotent(self, db):
        """038：终态任务重复取消幂等。"""
        org = _org(db)
        repo = AITaskRepository(db)
        task = repo.create(org.id, "ideas", {})
        repo.set_status(task.id, "running")
        repo.set_status(task.id, "succeeded", result={"ok": True})
        queue = TaskQueue(get_session_factory())
        assert queue.cancel(task.id) == "succeeded"

    def test_task_org_isolation(self, db):
        """048：任务按 org 隔离查询。"""
        org1 = _org(db, "组织A")
        org2 = _org(db, "组织B")
        repo = AITaskRepository(db)
        t1 = repo.create(org1.id, "ideas", {})
        # 用 org2 查不到 org1 的任务
        assert repo.get(t1.id, org2.id) is None
        assert repo.get(t1.id, org1.id) is not None
        assert repo.list_by_org(org2.id) == []

    def test_set_status_illegal_raises(self, db):
        """036：非法流转在 set_status 入口被拒。"""
        org = _org(db)
        repo = AITaskRepository(db)
        task = repo.create(org.id, "ideas", {})
        with pytest.raises(IllegalTaskTransition):
            repo.set_status(task.id, "succeeded")  # pending 不能直达 succeeded

    def test_get_404(self, db):
        repo = AITaskRepository(db)
        with pytest.raises(EntityNotFound):
            repo.get_or_404("不存在的id")
