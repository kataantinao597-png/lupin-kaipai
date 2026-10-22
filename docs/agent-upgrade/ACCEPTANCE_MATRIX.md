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

## Phase 8：完整回归、质量评测（2026-10-21）

| ID | 验收项 | 测试命令/用例 | 预期结果 | 实际结果 | 状态 | 证据路径 |
|---|---|---|---|---|---|---|
| P8-UNIT-01 | 边界单元测试：brief 解析/透明默认/merge、modify 解析修复上限/保护阈值/版本上限、orchestrator 取消/429 闩锁/模型步上限、quality_gates schema/事实引用/持久化 | `pytest tests/test_agent_boundary_phase8.py`（49 用例） | 全绿 | 49/49 通过 | PASS | `tests/test_agent_boundary_phase8.py` |
| P8-E2E-01 | E2E 完整闭环：一句话 brief→建 run/作品→新 session 重读持久化→create_run+set_plan→真实 execute_run_steps（真实 adapters）→AgentStep/会话消息→局部修改→版本恢复→export-json | `pytest tests/test_agent_e2e_phase8.py::TestAgentE2E` | completed 全链路，真实持久化断言 | 1/1 通过；3 步骤落盘；仅 copy 变化；恢复后一致；导出含快照 | PASS | `tests/test_agent_e2e_phase8.py` |
| P8-FAULT-01 | 故障演练：模型超时/429 硬停复核/5xx 不重试/可选→partial 必需→failed/幂等重复提交/用户取消/乐观锁冲突 | `pytest tests/test_agent_e2e_phase8.py::TestFaultDrills`（7 用例） | 全部按设计语义，无重试风暴 | 7/7 通过 | PASS | `tests/test_agent_e2e_phase8.py` |
| P8-EVAL-01 | 固定评测集 25 场景（正常 7/边界 9/失败 3/对抗 6），mock 模型可重复 | `./venv/bin/python scripts/eval_phase8.py` | 25/25 | 25/25 通过 | PASS | `scripts/eval_phase8.py`，`docs/agent-upgrade/EVALUATION.md`，`docs/agent-upgrade/eval_phase8_results.json` |
| P8-MET-01 | 质量指标实测（任务书 §十二 7 项） | 见 EVALUATION.md | 全部达标 | 核心闭环 100%（3/3）；工具白名单 13/13；越权/高风险事实/无关变化/重复扣费/UI 不一致均为 0 | PASS | `docs/agent-upgrade/EVALUATION.md` |
| P8-FIX-01 | 快照淘汰方向修复：超 50 个快照时删最旧（原代码删最新，与注释相悖） | `test_snapshot_version_cap` | 50 上限，淘汰最旧 | 通过；`works.py::_snapshot_work` 同修 | PASS | `backend/services/agent/modify.py`，`backend/api/v1/works.py` |
| P8-FIX-02 | 高风险关键词补齐"血压/血糖"（"降血压"原先漏检） | `test_fact_grounding_blood_sugar_keyword` + 评测 E13 | 拦截并标待核实 | 通过 | PASS | `backend/services/agent/quality_gates.py` |
| P8-REG-01 | 全量回归 | `./venv/bin/python -m pytest tests/ -q -p no:cacheprovider`（单次，scratch DB，558.73s） | 失败仅为 6 个基线陈旧用例 | **6 failed、721 passed、1 skipped**；6 个失败恰为基线陈旧用例（test_delivery_check×2、test_feasibility_fallback、test_handover_regressions×2、test_srt_route），无新增失败 | PASS | `docs/agent-upgrade/phase8_full_regression.txt` |
| P8-DB-01 | 业务库零写入（Phase 8 新增测试） | md5 比对 backend/data/lupin.db（Phase 8 测试运行前后） | 无变化 | Phase 8 自身测试/评测前后 md5 一致（2a5787a5…），零写入；注：全量回归触发了既有噪音测试往业务库写 16 行 ai_calls（模型名为 test-cache-*/test-breaker-* 等假名、task_id 为空），属已知问题"pytest 往真实 DB 写噪音行"（10-20 全量跑同样残留），非本次改动引入 | WARN | backend/data/lupin.db（只读） |
| P8-SEC-01 | 密钥扫描复核（含本次新增代码） | `./venv/bin/python scripts/scan_secrets_phase7.py --diff-only` | exit 0 | exit 0：无明文 key/secret/token、无 .env 引用 | PASS | `scripts/scan_secrets_phase7.py` |

**Phase 8 Gate 结论：PASS**（§十二硬性项全部通过）。
- 新增测试 57 个（边界 49 + E2E/故障演练 8）全绿；评测 25/25；7 项质量指标全部达标（含 5 个零指标）。
- 全量回归 721 通过，失败仅 6 个基线陈旧用例（与本次改动无关）。
- 评测发现 2 个真实缺陷已修复（快照淘汰方向、高风险关键词漏检），均有回归测试锁定。
- WARN 项：全量回归时既有噪音测试往业务库写 16 行 ai_calls（已知问题，非本次引入；Phase 8 自身测试零写入）。
- 遗留：评测为 mock 驱动（非生产流量）；用户级每日限额仍延后（Phase 7 已登记）；modify 未暴露 HTTP 路由，前端无新增用例。


---

## AI.md 端到端验收（2026-10-22，真机真模型）

> 按 `~/workspace/user/files/AI.md` 第 0–6 步执行。
> 环境：scratch 库 `~/workspace/accept-work/lupin_accept.db`（WAL），后端 127.0.0.1:8933，
> 模型 deepkey luna `gpt-6-luna`（保险箱 surrogate，用户授权本次验收使用），5 次真实调用无 429。
> 基线 `feat/agent-workspace-commercial` @ `961f501` + 验收修复 `8b4e79a`。
> 业务库 `backend/data/lupin.db` 只读，验收前后 ai_calls 均为 2197 行，零写入。

### 第 0 步：准备测试环境 — PASS

测试 org `acceptorg…000001` + editor/admin/隔离用户；真实产品章丘大葱（ZH-DC-2026，业务库只读复制）；
前端 /app 加载，左侧 12 个入口齐全（截图 `~/workspace/accept-shots/stage1-agent.png`）。

### 第 1 步：一句话生成完整项目

| 检查项 | 状态 | 证据 |
|---|---|---|
| 1A 任务计划来自真实后端（UI 路径） | **FAIL** | 前端 `agentSend()` 只调 POST /runs 建任务不触发执行；run `fe2b224f…` 卡死 pending/queued。**缺口**：前端未接入 `/one-shot`（后端 2bd2fd9 已补路由） |
| 1A（API 路径） | PASS | POST /api/v1/agent/one-shot → task `96703e6a…`；11 步落盘 agent_steps 全 succeeded，run completed |
| 1B 真实业务工具被调用 | PASS | parse_brief/query_products/query_facts/generate_strategy/generate_ideas/generate_storyboard/shooting_checklist/check_risk/generate_platform_copy/check_delivery/patch_work；日志无密钥 |

### 第 2 步：成果完整性 — PASS

作品 `2dc2db9b…`「章丘大葱·AI 创作」v2：strategy/creative/storyboard(5 镜)/shooting_list/risk_report(passed)/copy 齐全；
约束一致（500 元/2 人/手机/无稳定器）；未核实事项标待核实 9 项，无编造。

### 第 3 步：保存与刷新恢复 — PASS

GET /api/v1/works/{id} 重读：title/version=2，全量 data keys 一致。

### 第 4 步：只改第 3 镜 — PASS

POST /api/v1/agent/modify 200；impacted_shots=[3]；shot1/2/4/5 逐字不变；version 2→3；rechecks_passed=true。
注：one-shot 未回填 conversation.work_id 致初调 404，测试库手动绑定后通过（生产缺口，建议修复）。

### 第 5 步：导出与回归 — WARN

DOCX 可导出（38KB，22 段+1 表）但「成片总时长 0s」「镜None」— 导出模板与 storyboard 结构映射错位。
原入口回归：产品资料 200、作品列表正常、左侧入口无覆盖。

### 第 6 步：异常 — PASS

幂等（同 key 同 task_id）、越权（跨组织 404）、取消（pending→cancelled，修复后）、日志无密钥均通过。

### 验收修复（commit 8b4e79a，3 项真实缺陷）

1. `run_one_shot_brief` 复用已 running 任务时重复置状态 → 非法转换 failed。修：仅 pending 才置 running。
2. `_parallel_read_steps` 误把 sessionmaker 当 Session → 11 步中断。修：lambda 包一层拿 Session。
3. `TaskQueue.cancel` 对未入队 pending 任务无效。修：pending 直接置 cancelled。
回归：orchestrator_phase5 27/27，e2e_phase8 8/8。

### 结论：FAIL

UI 路径「AI 创作助手输入框发需求」建出的 run 永不执行——阻断用户使用的核心链路缺陷。
API 路径全通。待前端接入 `/one-shot` 后重验第 1 步 UI 项即可转 PASS。
