# 执行日志（AI 对话式 Agent 升级）

> 分支：`feat/agent-workspace-commercial`（基线 `fc74d70`）
> 范围：Phase 0 → Phase 8（Phase 9 部署按用户指令跳过）
> DB：`backend/data/lupin.db` 为 2026-10-21 18:58 业务快照，**禁止覆盖**；测试一律用 scratch 拷贝
> 红线：429 硬停；密钥不进代码/日志/git；视频生成 key 未授权禁用

## Phase 0 — 基线

| 步骤 | 命令/动作 | 结果 |
|---|---|---|
| 工作区确认 | `git status` 干净，分支 `feat/agent-workspace-commercial`，HEAD `fc74d70` | PASS |
| 备份 | `src-fc74d70-20261021-2127.tgz`（30MB）+ `lupin.db.pre-agent-work` → `~/workspace/your_files/lupin-agent-upgrade-backup/` | PASS |
| 导入检查 | `./venv/bin/python -c "import backend.app"` | PASS（仅 JWT 未配置 warning，开发环境预期） |
| 路由清单 | app.routes 去重 181 条；`/api/health`、`/api/v1/ai-tasks`、`/api/v1/works`、`/api/v1/auth/login` 均存在 | PASS |
| 基线 pytest | `DATABASE_URL=sqlite:////tmp/lupin-baseline-test.db ./venv/bin/python -m pytest tests/ -x -q`（scratch DB，不写真实库） | 见下 |

### 基线 pytest 结果（2026-10-21 21:35）
- 命令：`DATABASE_URL=sqlite:////tmp/lupin-baseline-test.db ./venv/bin/python -m pytest tests/ -q -p no:cacheprovider`
- 结果：**6 failed, 500 passed, 1 skipped**，耗时 437s
- 6 个失败全部是已知陈旧用例（与本次改动无关，基线即如此）：
  - `test_delivery_check.py::DeliveryCheckTests::test_delivery_check_is_data_driven_and_blocks_unconfirmed_risks`
  - `test_delivery_check.py::DeliveryCheckTests::test_delivery_check_marks_empty_project_as_action`
  - `test_feasibility_fallback.py::FeasibilityFallbackTests::test_model_failure_returns_rule_results_without_502`
  - `test_handover_regressions.py::HandoverRegressionTests::test_health_reports_configured_model`
  - `test_handover_regressions.py::HandoverRegressionTests::test_regenerate_shot_declares_request_context`
  - `test_srt_route.py::SrtRouteTests::test_export_srt_returns_utf8_subtitle_file`
- 真实 DB 未被写入（scratch 拷贝验证：`backend/data/lupin.db` mtime 未变）
- Phase 0 Gate：**PASS**（基线干净，进入 Phase 1）

## Phase 1 — 资产盘点与设计（子代理执行，只读审计）

| 交付物 | 状态 |
|---|---|
| `docs/agent-upgrade/TOOL_MAPPING.md`（8 逻辑工具 + 6 扩展工具，每行真实文件/函数/行号证据） | ✅ 落盘 |
| `docs/agent-upgrade/PHASE1_DESIGN.md`（复用/适配/缺失清单、工具层位置 `backend/services/agent/`、执行模型/权限/幂等/成本归属、UI 约束） | ✅ 落盘 |

关键设计决策：
1. 工具层放 `backend/services/agent/`（tools.py/context.py/adapters.py/planner.py），不污染 gateway，不进路由层
2. 新增表最小化：只加 `conversations` / `conversation_messages` / `agent_steps`；run 复用 `AITask(task_type="agent_run")`，不新建 agent_runs 表
3. 无 function calling → "结构化 JSON 计划 + 后端白名单分派"；状态机扩展用 payload_json 暂存，不改表
4. 用户级每日限额由另一 agent 实现，本分支只在 context.py 预留 `check_user_quota()` 桩位
- Phase 1 Gate：**PASS**（进入 Phase 2）

## Phase 2 — 工具层实现（子代理执行，commit 54e963e）

### 改动文件
- `backend/services/agent/tools.py` — 14 工具注册表（8 逻辑 + 6 扩展）：Pydantic v2 schema（extra=forbid）、白名单校验、工具分级（patch_work/delete_work 为确认级）
- `backend/services/agent/context.py` — 只读上下文装配（跨组织对象返回 None 防枚举）；`check_user_quota()` 桩位（enforced=False + TODO，Phase 7 对接）
- `backend/services/agent/adapters.py` — 14 适配器 + `run_tool` 统一分派：只调既有 service/repository，无直写 DB；统一信封；权限每次从 Membership 重读；patch_work 乐观锁；regenerate_shot 检查 Shot.locked
- `backend/services/agent/planner.py` — JSON 计划校验：白名单/params schema/depends_on 无环/run 级上限（25 步 / 8 模型步）
- `migrations/versions/0020_agent_tables.py` — conversations / conversation_messages / agent_steps（SQLite/Postgres 兼容，幂等，downgrade 验证）
- `tests/test_agent_tools_phase2.py` — 38 个测试

### 测试结果
- 新测试：38/38 通过
- 相关既有测试无新增失败：test_db_phase3 14✓、test_ai_observability 12✓、test_ai_gateway_ext 22✓、test_async_tasks_phase4 22✓
- 真实 DB 未触碰（alembic 仍 0019，无 agent 表）；全程无真实模型调用
- 发现（非阻塞）：`backend.models` 循环导入坑（须先 import backend.auth.*），已在 agent/__init__.py 固定顺序并注释
- Phase 2 Gate：**PASS**（进入 Phase 3）

## Phase 3 — 对话持久化与任务控制（子代理执行，commit 6314062）

### 改动文件
- `backend/services/agent/conversations.py`（新建）：Conversation/ConversationMessage/AgentStep ORM + CRUD，org 归属校验（404 防枚举），role 限 user/assistant/tool，消息分页
- `backend/services/agent/runner.py`（新建）：create_agent_run / set_run_plan / execute_run_steps / run_agent_run（queue runner 签名）/ confirm_step / get_run_state / step_params_digest
- `backend/ai_tasks/runners.py`：RUNNERS["agent_run"] 注册（函数内延迟导入避循环导入）
- `backend/models.py`：TASK_TYPE_AGENT_RUN="agent_run" 加入 TASK_TYPES
- `backend/ai_tasks/repository.py`：新增 update_payload()（整体替换 payload_json，不碰状态机）
- `backend/ai_tasks/queue.py`：_execute 识别 waiting_user 挂起信号（保持 running）；sweep_timed_out 跳过 waiting_user 的 agent run
- `tests/test_agent_run_phase3.py`（新建，25 用例）

### 测试结果
- 新测试：25/25 PASS；回归 test_async_tasks_phase4 22/22、test_agent_tools_phase2 38/38
- 全部 mock run_tool，零真实模型调用

### ⚠️ 事故记录（已恢复）
- 子代理调试迁移时用 `set_main_option("sqlalchemy.url", …)` 定向 scratch 库，被 `migrations/env.py:29` 的 DATABASE_URL 覆盖，一条 `alembic upgrade head` 误跑到业务库（0019→0020）
- 已立即 `downgrade 0019_ai_call_org` 恢复；我方独立验证：integrity ok、版本戳 0019、三表无残留、业务数据完好（48 用户/2197 ai_calls/16 作品）
- 教训：迁移类操作一律走 DATABASE_URL 环境变量定向；已记入 KNOWN_ISSUES.md
- Phase 3 Gate：**PASS**（进入 Phase 4）

## Phase 4 — AI 创作助手 UI（待派发，拆 4a 路由 + 4b 前端）
