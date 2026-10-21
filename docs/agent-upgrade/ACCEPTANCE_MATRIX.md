# 验收矩阵（AI 对话式 Agent 升级）

> 按任务书 §十四维护：每次测试后填实际结果。状态只能是 `PASS` / `FAIL` / `WARN` / `BLOCKED` / `NOT_RUN`。证据为日志路径、测试报告、响应摘要与相关 commit，不含秘密凭据。

## Phase 7：商用级安全、成本和可观测性（2026-10-21）

| ID | 验收项 | 测试命令/用例 | 预期结果 | 实际结果 | 状态 | 证据路径 |
|---|---|---|---|---|---|---|
| P7-SEC-01 | 跨租户：A 组织用户读/写 B 组织 conversation/work/agent run 被拒 | `pytest tests/test_agent_security_phase7.py::TestCrossTenant`（9 用例） | 全部 404/403 等价拒绝，0 越权 | 9/9 通过：create/get/confirm/set_plan/run_tool/get_task_status/check_delivery/非成员/modify 均拒绝 | PASS | `tests/test_agent_security_phase7.py` |
| P7-SEC-02 | 提示注入不得改变工具白名单与权限规则 | `pytest tests/test_agent_security_phase7.py::TestPromptInjection`（5 用例） | 恶意 brief/修改请求下白名单不变、越权 params 被拒 | 5/5 通过：exec_shell 白名单外被拒；params 塞 org_id 被拒；hostile plan 非法 operation 抛 ModifyParseError | PASS | `tests/test_agent_security_phase7.py` |
| P7-SEC-03 | 上限 enforcement：25 步/8 模型步/单步超时/总时间上限 | `pytest tests/test_agent_security_phase7.py::TestLimits`（5 用例） | 超限计划被拒；超时进 partial/failed 且保留成果 | 5/5 通过：26 步/9 模型步被拒；单步超时 failed（step 行保留）；总时间超限 partial（time_budget_exceeded） | PASS | `tests/test_agent_security_phase7.py`，`backend/services/agent/runner.py` |
| P7-SEC-04 | 密钥扫描：新增代码 + git diff 无明文 key/secret/token、无 .env 引用 | `./venv/bin/python scripts/scan_secrets_phase7.py` 及 `--diff-only`；`TestSecretScan`（3 用例） | 退出码 0；错误响应不含 key 片段 | 双模式均 exit 0；超长 key/冲突错误的 message 不回显 key 本体 | PASS | `scripts/scan_secrets_phase7.py` |
| P7-IDEM-01 | DB 级幂等键表：迁移 0021 建表，回放/冲突/过期/并发语义与内存版一致 | `pytest tests/test_agent_security_phase7.py::TestIdempotencyDB`（4 用例）+ `pytest tests/test_v21_idempotency.py` | 4/4 通过；v21 全绿 | 重启（reset_engine）后重放命中；并发双击模型只执行一次；org_id 落盘；agent run 同 key 不重复建任务 | PASS | `migrations/versions/0021_idempotency_keys.py`，`backend/core/idempotency.py` |
| P7-IDEM-02 | agent run 创建路由接 Idempotency-Key（4a 遗留） | `TestIdempotencyDB::test_agent_run_create_idempotent` | 同 key 同体第二次回放，不重复建任务 | task_id 一致且带 idempotent_replay 标记；AICall/AITask 无重复行 | PASS | `backend/api/v1/agent.py::create_run` |
| P7-RL-01 | agent 路由限流：ai 档 20/分钟/用户，超限 429 | `pytest tests/test_agent_security_phase7.py::TestRateLimit`（2 用例） | 前 20 放行、第 21 个 429 + Retry-After + 标准错误信封 | 2/2 通过 | PASS | `backend/security/rate_limit.py` |
| P7-COST-01 | 每个 agent run 的 ai_calls 有 task_id + org_id；单 run 成本可按 ai_ledger 口径汇总 | `pytest tests/test_agent_security_phase7.py::TestCostAttribution`（2 用例） | gateway 绑定 task_id + _org_id；ledger 按 org 隔离、按 task 聚合 | 2/2 通过：(2,300,15) 汇总正确；跨组织 0 泄漏 | PASS | `tests/test_agent_security_phase7.py` |
| P7-COST-02 | 429 穿透硬停（agent 路径） | `pytest tests/test_agent_orchestrator_phase5.py::TestRateLimit::test_429_hard_stop`（复核） | 撞 429 立刻停，不重试不换端点 | 通过（回归 50/50 中的一项） | PASS | `tests/test_agent_orchestrator_phase5.py` |
| P7-QUOTA-01 | 用户级每日限额对接状态 | 检查 `check_user_quota` 实现 | 如另一 agent 已实现则对接，否则登记延后 | 仍为桩位（enforced=False）；**未自行实现**；KNOWN_ISSUES 已登记"延后（等用户点头），Phase 7 未对接" | PASS | `docs/agent-upgrade/KNOWN_ISSUES.md`，`backend/services/agent/context.py` |
| P7-REG-01 | 回归：phase2/3/4a/5/6 相关测试全绿 | `pytest tests/test_agent_tools_phase2.py tests/test_agent_run_phase3.py tests/test_agent_api_phase4a.py tests/test_agent_orchestrator_phase5.py tests/test_agent_modify_phase6.py tests/test_v21_idempotency.py` | 全绿（基线 6 个陈旧失败除外） | 47+25+50+21 全绿；v21 全绿；6 个基线陈旧失败与本次无关 | PASS | 各测试文件 |
| P7-DB-01 | 业务库零写入 | 比对 `backend/data/lupin.db` 测试前后哈希/版本戳/行数 | 无变化 | scratch 拷贝迁移验证；真实库版本戳 0019、ai_calls 2197、works 16 未变（见回归命令输出） | PASS | `backend/data/lupin.db`（只读） |

**Phase 7 Gate 结论：PASS**（§十一硬性项全部通过；用户级限额为已知延后项，不阻塞发布前置条件之外的项）。
