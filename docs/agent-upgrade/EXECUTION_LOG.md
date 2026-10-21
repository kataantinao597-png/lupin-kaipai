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

## Phase 4 — AI 创作助手（4a 路由已完成，commit e79246b）

### 4a 改动文件
- `backend/api/v1/agent.py`（新建 ~290 行）：6 端点（runs 创建 202/查询/plan/confirm/cancel/messages 分页），全部委托 services 层；成功标准信封，错误按 error_codes.py（4002/1001/4040/4000/4010）
- `backend/app.py`：_mount(agent_router, prefix="/api/v1")（+2 行）
- `tests/test_agent_api_phase4a.py`（新建，23 用例）

### 4a 测试结果
- 新测试 23/23；回归 phase3 25/25、phase2 38/38；业务库零写入（integrity ok，mtime 早于本任务）
- 遗留：confirm approved 同步执行剩余步骤（HTTP 可能阻塞，Phase 5 改异步）；创建 run 未接 Idempotency-Key（Phase 7 补）
- Phase 4a Gate：**PASS**

### 4b 前端 UI（子代理已完工，待 Gate）
- 范围：侧边栏「AI 创作助手」+ 首页「AI 一句话创作」+ 对话视图/计划侧栏 + 专业页轻量入口 + Playwright 三视口截图
- 改动文件：
  - `frontend/app.html`：侧边栏新增 `data-v="agent"` 导航项（复用 .nav-item，置工作台之后）；v0 新增「AI 一句话创作」入口卡片（`#agent_entry`，原「新建作品」保留）；新增 `#agent` 视图（对话区左/主 + 执行计划侧栏右，输入栏底部固定 + safe-area）；v2/v3（创意/分镜/风险）/v5 各加一个 `data-agent-src`「✦ 让 AI 处理当前内容」轻量按钮；新增 agent 专属 CSS（复用现有色板/组件类，无新框架）
  - `frontend/js/app.js`：新增 Phase 4b 模块（~430 行）：NEED_AUTH 加 `agent`、`go("agent")` 走现有路由；真实状态机 idle/发送中/排队中/运行中/等待确认/部分完成/失败/已取消/已保存（完成且 summary 持久化）全部来自 2.5s 轮询 `GET /api/v1/agent/runs/{id}`，无固定延迟模拟；waiting_user 渲染确认卡片（步骤说明 + 同意/拒绝 → POST confirm）；取消/重新发起/查看错误详情（可展开 JSON）均接真实接口；成果卡片「去继续编辑」经 `openWork(work_id, view)` 跳回专业页；五处入口经 `openAgent(src)` 带真实 product_id/work_id/shot_id（仅传 ID）；`V4B-TEST-MARKER` 只读探针 `window.__agentProbe` 供 Playwright 读取（无写逻辑）
- 验证（Playwright + Firefox，三视口，scratch DB `/tmp/lupin_4b_scratch.db`，业务库零触碰：2197 ai_calls、版本 0019）：
  - 42/42 通过（连续两次全绿）：导航回归 v0–v9+agent（active 高亮正确、无重复菜单 11 项）；v0 入口卡片 + 新建作品保留；五处入口均落地助手视图且上下文 chip 含真实 work_id；发送→排队中（真实轮询）；waiting_user 确认卡片（真实 plan + 真实 execute_run_steps 挂起）；同意→已保存（patch_work 真实执行）；成果卡片跳转 v2；失败态（parse_brief 需模型真实失败）+ 错误详情展开；取消按钮调真实 cancel API（4a 语义：未入队 run 只打协作式标记，状态保持排队）；拒绝确认→已取消（真实 confirm 路径）；1440/768/390 零横向溢出；768/390 计划区默认折叠可展开；390 输入栏在视口内、抽屉含 AI 助手入口；全程零页面 JS 错误
  - 截图：`docs/agent-upgrade/screenshots/phase4b/`（1440: idle/queued/waiting-user/saved/failed/cancelled；768/390: idle）
  - 修过的真 bug：① 空闲态误显示「取消任务」（setAgentButtons 把未知态归一化成 queued）；② 历史确认卡片按钮在重渲染后被冲掉冻结态（改为渲染时按 `AGENT.state`/`tid` 直接决定 disabled，旧卡片默认冻结；点击旧卡片另有 tid 校验挡下）
- 遗留（非阻塞，Phase 5/后续）：① 顶栏「取消任务」对排队中 run 调 cancel API 后状态保持排队（4a 测试 `test_cancel_pending` 已将此定为预期语义：未入队只打标记，Phase 5 入队后协作式取消）；② confirm approved 仍同步执行剩余步骤（4a 遗留，HTTP 可能阻塞，Phase 5 改异步）；③ 每次发送新建 conversation（多轮连续对话的上下文串联待 Phase 5 规划器）
- Phase 4b Gate：**PASS**（进入 Phase 5）

## Phase 5 — 一句话生成编排（子代理执行中，2026-10-21 22:50）
- 范围：brief 解析器（自然语言→结构化 Brief，网关真实模型调用）+ orchestrator.py（11 步典型流程编排）+ 质量门禁（artifact schema/一致性/风险引用/保存后重读）+ confirm 改异步 + 10+ 场景测试
- 红线：vault luna/gpt 可调用（429 硬停），视频 key 禁用，scratch DB

### Phase 5 完工记录（2026-10-21 23:xx，子代理）
- 新增：`backend/services/agent/brief.py`（Brief schema/透明默认值/待核实/≤2 次修复/merge_brief）、`orchestrator.py`（run_one_shot_brief 11 步：brief→产品/事实→策略→创意→分镜→拍摄清单→风险→有限修复→文案/交付→写回作品→摘要；每步 run_tool 白名单分派；只读可并行/写串行；task_id 成本归属；25 步/8 模型步上限；429 硬停）、`quality_gates.py`（artifact schema/一致性/风险引用/保存后重读）
- 新增工具：`generate_strategy`、`generate_platform_copy`（白名单 14→16；test_agent_tools_phase2.py 断言已同步更新，非放水）
- 修复 4a 遗留：`confirm_step` approved 只落确认态，路由层 `_enqueue_run_resume` 重新入队、worker（run_agent_run）异步恢复；`queue._execute` 容忍已 running 任务重入（跳过 running→running 非法流转）
- 修复 4b 遗留③：`create_agent_run(conversation_id=...)` 复用会话 + brief 增量合并；路由新增 `conversation_id` 参数；前端 `agentSend` 回传 `AGENT.convId`
- 修过的真 bug：① shooting_checklist/check_delivery 用 validator 完整 schema 校验工具链简化分镜（shot_no/duration_sec）必报"结构非法"——adapters 新增 `_normalize_storyboard_for_validation` 归一化（幂等）；② patch_work 白名单缺 Phase 5 artifact 键（strategy/creative/shooting_list/risk_report/copy）——已补；③ quality_gates `_issue` 把 detail 双重嵌套导致修复定位不到句子——已修；④ brief 透明默认标记在 Pydantic 填充后无法区分"未指定"——改为按模型原始输出判定
- 控制流异常穿透：`ModelStepsExceeded`/`RateLimitedStop` 在 adapters 定义，run_tool 与各 adapter 的 `except Exception` 一律 re-raise，不被转成 ToolResult 吞掉
- 测试：`tests/test_agent_orchestrator_phase5.py` 27/27 通过（正常/缺失字段/无效 JSON 修复+超限/单工具失败/风险拦截标待核实/低预算单人/重读一致/可选失败→partial/模型步上限→partial/429 硬停/confirm 异步/service+HTTP/多轮复用/brief 单元/门禁单元/HTTP 接线）；回归 test_agent_api_phase4a.py + test_agent_run_phase3.py + test_agent_tools_phase2.py 86/86，test_async_tasks_phase4.py + test_db_phase3.py 36/36；业务库零写入（全程 scratch）
- 真实冒烟（luna key 经保险箱 surrogate，走 backend/ai/gateway.py；key 未入代码/日志）：brief 解析 1 次，模型 gpt-6-luna，耗时 19248ms，attempts=1，prompt 4694 / completion 323 / total 5017 tokens，费用约 6 分；产地/认证/功效/价格/销量均正确标"待核实"，未臆造；无 429
- 遗留（非阻塞）：① orchestrator 目前是同步 service 函数，HTTP 一句话入口路由未建（Phase 6/后续按需加，前端 4b 对话流已可经 run/create+plan 调用）；② check_delivery 的"产品资料/风险审核"阻断项沿用旧语义（orchestrator 只记录不强拦）；③ 4b 遗留①（排队中 run 取消语义）未动
- Phase 5 Gate：**PASS**（§5.4 十场景全覆盖：正常/缺失字段/无效 JSON/单工具失败/风险内容/低预算单人/重读一致/可选失败→partial/confirm 异步/多轮串联）

## Phase 5 — 一句话生成编排（子代理执行，commit 2634b06）
- 新增：brief.py（Brief 解析器，Pydantic schema，关键事实不明强制待核实）/ orchestrator.py（run_one_shot_brief，11 步流程）/ quality_gates.py（artifact schema/一致性/风险引用/保存后重读）
- 修改：tools.py（+generate_strategy/+generate_platform_copy，白名单 14→16）/ adapters.py（parse_brief 走完整 schema，修分镜校验真 bug）/ runner.py（confirm 改异步 + conversation 复用 + brief 增量合并）/ api/v1/agent.py（confirm 入队恢复 + conversation_id）/ ai_tasks/queue.py（running 重入容忍）/ frontend/js/app.js（多轮串联回传 convId）
- 测试：新 27/27；回归 135/135 全绿（phase2/3/4a 旧测试按新语义更新）
- 真实冒烟：gpt-6-luna 经保险箱，brief 解析 19.2s，5017 tokens（约 6 分），待核实项未臆造，无 429
- 遗留：HTTP"一句话"入口路由未建（后续按需）；用户级限额仍是桩位
- Phase 5 Gate：**PASS**（进入 Phase 6）

## Phase 6 — 连续对话与局部修改（子代理执行中）
