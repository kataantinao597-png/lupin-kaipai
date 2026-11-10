"""超时口径集中常量表（O-09，CASE-20261109-005）。

只搬常量、写清“从何时起算”，数值一个字都没改。原先分散在
``ai_tasks/queue.py``、``services/agent/runner.py``、
``services/poster_jobs.py``、``api/v1/poster_image.py`` 四处，
下次再改超时只改这里，各模块 import 同一份常量，防口径漂移。

本模块零 backend 依赖（只含常量），放哪里都不会循环导入：
``queue.py`` 与 ``runner.py`` 之间已有互相引用，常量绝不能放
在他俩任何一处。

各口径的起算点（以代码实测为准，不是约数）：
- TASK_TIMEOUT_SECONDS：通用任务。pending 排队超时自 ``created_at``
  起算；running（非 agent_run）自 ``started_at`` 起算，
  ``started_at`` 为空的老数据回退 ``created_at``。
- AGENT_RUN_TIMEOUT_SECONDS：agent_run 的 running 超时，自
  ``started_at`` 起算（为空回退 ``created_at``）；数值与
  MAX_RUN_TOTAL_SECONDS 对齐（B-03），两者是同一 30 分钟预算
  的两处表达，不是两个独立预算。
- STEP_TIMEOUT_SECONDS：单步执行超时，自该步工具调用开始起算。
- MAX_RUN_TOTAL_SECONDS：run 总执行预算，自本次
  ``execute_run_steps`` 开始执行起算（每次恢复执行重新计时，
  不是自任务 ``created_at`` 起算）。
- JOB_TIMEOUT_SECONDS：海报看门狗，自 ``started_at`` 起算；尚未
  开始执行的排队任务回退 ``created_at`` 计时（B-09）。与上游
  2×90s=180s 是紧配合（只留 20s 余量），数值绝不能“顺手统一”。
- POSTER_REQUEST_TIMEOUT_SECONDS：海报上游单通道调用上限，自单次
  调用发出起算；主备最坏 2×90s=180s，与看门狗 200s 对齐。
- POSTER_SYNC_TOTAL_TIMEOUT_SECONDS：同步接口主备共享总预算，自
  请求发出起算（deadline 切分，主失败后备选只剩剩余预算）。
"""
from __future__ import annotations

#: 通用任务超时（秒）：pending 排队 / running（非 agent_run）共用。
#: 起算：pending 自 created_at；running 自 started_at（空则 created_at）。
TASK_TIMEOUT_SECONDS = 600

#: agent_run 的 running 超时（秒），与 MAX_RUN_TOTAL_SECONDS 对齐。
#: 起算：自 started_at（空则 created_at）。
AGENT_RUN_TIMEOUT_SECONDS = 1800

#: agent run 单步执行超时（秒）。起算：自该步工具调用开始。
STEP_TIMEOUT_SECONDS = 5 * 60

#: agent run 总执行预算（秒）。起算：自本次 execute_run_steps 开始执行。
MAX_RUN_TOTAL_SECONDS = 30 * 60

#: 海报任务看门狗（秒）。起算：自 started_at（未开始执行则回退
#: created_at）。与上游 2×90s=180s 紧配合，勿改数值。
JOB_TIMEOUT_SECONDS = 200

#: 海报上游单通道调用上限（秒）。起算：自单次调用发出。
POSTER_REQUEST_TIMEOUT_SECONDS = 90.0

#: 海报同步接口主备共享总预算（秒）。起算：自请求发出。
POSTER_SYNC_TOTAL_TIMEOUT_SECONDS = 55.0
