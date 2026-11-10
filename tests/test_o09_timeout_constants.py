"""O-09：超时口径集中常量表测试（CASE-20261109-005）。

只验两件事：①数值一个字都没改；②各模块引用的是同一份常量
（改 backend/core/timeouts.py 一处即全局生效，防口径漂移）。
"""
import pytest

pytestmark = pytest.mark.unit


def test_timeout_values_unchanged():
    from backend.core import timeouts as t
    assert t.TASK_TIMEOUT_SECONDS == 600
    assert t.AGENT_RUN_TIMEOUT_SECONDS == 1800
    assert t.STEP_TIMEOUT_SECONDS == 300
    assert t.MAX_RUN_TOTAL_SECONDS == 1800
    assert t.JOB_TIMEOUT_SECONDS == 200
    assert t.POSTER_REQUEST_TIMEOUT_SECONDS == 90.0
    assert t.POSTER_SYNC_TOTAL_TIMEOUT_SECONDS == 55.0


def test_modules_reference_the_same_constants():
    from backend.core import timeouts as t
    from backend.ai_tasks import queue as q
    from backend.services.agent import runner as r
    from backend.services import poster_jobs as pj
    from backend.api.v1 import poster_image as pi
    assert q.TASK_TIMEOUT_SECONDS == t.TASK_TIMEOUT_SECONDS
    assert q.AGENT_RUN_TIMEOUT_SECONDS == t.AGENT_RUN_TIMEOUT_SECONDS
    assert r.STEP_TIMEOUT_SECONDS == t.STEP_TIMEOUT_SECONDS
    assert r.MAX_RUN_TOTAL_SECONDS == t.MAX_RUN_TOTAL_SECONDS
    assert pj.JOB_TIMEOUT_SECONDS == t.JOB_TIMEOUT_SECONDS
    # 海报 200s 看门狗与上游 2×90s=180s 的紧配合仍在（只留 20s 余量）
    assert (t.JOB_TIMEOUT_SECONDS
            - 2 * t.POSTER_REQUEST_TIMEOUT_SECONDS) == 20
    assert pi.REQUEST_TIMEOUT == t.POSTER_REQUEST_TIMEOUT_SECONDS
    assert pi.SYNC_TOTAL_TIMEOUT == t.POSTER_SYNC_TOTAL_TIMEOUT_SECONDS
