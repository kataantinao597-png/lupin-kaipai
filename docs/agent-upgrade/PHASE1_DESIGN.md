# PHASE1_DESIGN.md — Phase 1 实现设计（复用/适配/缺失 + 架构决策）

> 只读审计结论。分支 `feat/agent-workspace-commercial`。未写业务代码、未连 DB、未跑测试。
> 详细证据见 `TOOL_MAPPING.md`。本文件只做设计判断。

## 1. 三类清单

### 1.1 直接复用（不动或只调参数）

1. **AI 调用底座 `AIGateway`**（`backend/ai/gateway.py:67`）：预算/配额/重试/单任务熔断（5 次/500K token）/配置记录/可观测性全套已生效。Agent 的每一步模型调用都走 `gateway.chat()` 或 `generate_with_retry()`，**不自建调用通道**。注意 OpenAI 兼容层（`deepkey_adapter.py`）声明无 function calling，因此编排走"结构化 JSON 计划 + 后端白名单分派"（任务书 §2.3 允许的路线）。
2. **`AITask` 异步任务全套**（模型 `models.py:566` + `api/v1/ai_tasks.py` + `ai_tasks/{queue,repository,runners,state_machine}.py`）：202 创建/轮询/取消/超时扫描/状态机强制流转/`Idempotency-Key` 幂等。Agent run 直接建模为一种 `task_type`（如 `agent_run`），零新增表即获得任务控制。
3. **风险/可行性/交付检查**：`risk_service.check_storyboard_risk`（AI + 确定性事实 grounding 规则层）、`feasibility` v1/v2、`delivery_service.check_delivery`、`delivery_gate_service`（门禁+豁免）。"高风险事实必须引用或标待核实"的质量门禁可直接建立在现有 `flags` 输出上。
4. **作品版本体系**：`WorkVersion` 快照/恢复（`works.py:451/478`）+ `Work.version` 乐观锁 + `Shot.locked` 镜头锁定。Agent 的"局部修改、锁定保护、版本恢复"需求已有数据结构支撑。
5. **RBAC**（`backend/rbac.py`）：5 角色矩阵 + `require_work_access`（防 IDOR）+ 组织管理硬限制。Agent 工具层不发明新权限模型，直接复用。

### 1.2 需要适配（复用 + 包一层）

1. **创意/分镜/文案/拍摄单等业务 API**：现有接口是"单次调用、面向人机交互"的（如 `POST /api/ideas`、`POST /api/storyboard`、`POST /copy/docs/{doc_id}/optimize`）。Agent 需要机器可编排的形态：统一入参（含 `work_id/org_id/user` 上下文）、统一结果信封（`status/data/artifact_ref/warnings/error_code`，任务书 §2.2）、写回作品/版本。适配层做参数装配 + 结果落盘，**不复制业务逻辑**。
2. **作品局部更新**：`PATCH /works/{work_id}` 是全量 `data_json` 更新语义。Agent 的"只改第 3 个镜头/只改口播"需要工具层实现 patch 语义：读当前版本 → 只写目标字段/目标 artifact → 版本号校验提交。防止无关内容被重写（任务书硬性指标）。
3. **成本归属到 run 维度**：`ai_calls` 已有 `task_id/org_id` 列，用量口径 `ai_ledger.org_calls_query` 已统一。Agent run 的每步 gateway 调用传入 `task_id=agent_run 的 AITask.id`，即可按 run 汇总 token/成本/调用次数，无需新表。
4. **`colloquial_rewrite` 与拉呱风**：能力现成，Agent 的"山东本土特色"改写需求直接调它；适配点是把它纳入工具白名单并约束输出长度。
5. **预算生效前提**：`get_ai_gateway` 的 org 解析依赖 `X-Org-Id` 头或 `?org_id=`（`core/deps.py:120-135`）；同步调用若前端未声明 org 则预算静默跳过。Agent 服务端编排**必须显式绑定 org**（走 `bind_org()`，参考 runner 的 `_org_id` 注入模式），不能依赖前端传头。

### 1.3 真缺失（必须新建）

1. **Agent 工具层**：`backend/services/agent/` 不存在。新建：`tools.py`（工具定义/schema/注册表/白名单）、`context.py`（装配用户/组织/作品/产品/事实上下文）、`adapters.py`（每个工具的适配器：调既有 service/repository，**不直写 DB**，不绕过业务规则）。工具分级 `core_required/optional/user_confirmation_required`（破坏性/高成本操作必须用户确认）。
2. **对话持久化最小表**：`conversations`（id/org_id/user_id/work_id/brief_json/status/created_at）+ `conversation_messages`（id/conversation_id/role/content_json/created_at）。`AITask` 可承载 run，但多轮消息历史无处可放，这是最小新增。
3. **Step 级执行记录**：`agent_steps`（id/task_id→ai_tasks.id/tool_id/params_digest/output_ref/status/error/started_at/finished_at/duration_ms）。理由：任务书 §2.2 要求每个工具调用记录执行时间/工具版本/错误类别；`AICall` 只记模型调用不记工具调用，且工具可能不调模型（如确定性交付检查）。挂在 `ai_tasks.id` 下，不另建 run 表。
4. **用户级每日限额**：`quota_service` 只有组织维度。用户 2026-10-21 已定"普通用户每天 5 次"并交由另一 agent 实现——本分支未见实现。工具层设计时预留检查点（`agent/context.py` 内 `check_user_quota()` 桩位），**不假设它已存在**，Phase 7 联调时对接。
5. **DB 级幂等键表**：`core/idempotency.py` 是进程内存实现，多 worker/重启失效。Agent"重复提交不重复建作品/不重复扣费"是发布硬指标，Phase 7 需补 `idempotency_keys` 表（key/org_id/route/body_hash/status/response_json/created_at）。Phase 2-6 可先用内存版，但必须在 KNOWN_ISSUES 登记局限。

### 1.4 不需要新增的东西（明确说不）

- **不新建 Conversation/Run/Step 全套重型模型**：run = `AITask(task_type="agent_run")`，step = `agent_steps`，message = `conversation_messages`。三张小表 + 两张复用，足够。
- **不给 `Work` 加字段**：作品数据继续走 `data_json` + `WorkVersion` 快照。
- **不新造计费台账**：`ai_calls` + `model_prices` + `ai_budgets` + `billing_service` 已闭环（记录/估算/预算/账单），Agent 只需正确传 `task_id/org_id`。
- **不引入新 CSS 框架/新主题**：UI 约束见 §6。

---

## 2. 新增表/字段必要性结论

| 候选 | 结论 | 理由 |
|---|---|---|
| `conversations` | **必要**（最小） | 多轮对话历史无处可放；`AITask.payload_json` 只能存单轮 |
| `conversation_messages` | **必要**（最小） | 同上；role 限 `user/assistant/tool` 三种 |
| `agent_steps` | **必要**（最小） | 工具级审计是任务书 §2.2 硬性要求；`AICall` 覆盖不了非模型工具调用 |
| 新增 `agent_runs` 表 | **不必要** | `AITask` 已有 id/org_id/status/progress/result_json/error/cancel/priority/时间戳/model_used/prompt_version，完全覆盖 run 需求；`task_type="agent_run"` + `payload_json` 存 brief/plan 即 |
| `Work` 新字段 | **不必要** | `data_json` + `WorkVersion` + `version` 乐观锁已够 |
| 幂等键表 | Phase 7 **必要**，Phase 2-6 可暂缓 | 内存版覆盖单进程主力场景；发布前必须补 DB 版 |

---

## 3. Agent 工具层放在哪一层

**决策：`backend/services/agent/`（新建包），包含 `tools.py` / `context.py` / `adapters.py` / `planner.py`。**

理由：

1. **复用优先**：`backend/services/` 已有 8 个业务服务（quota/billing/delivery/delivery_gate/risk/review/webhook/ai_ledger），约定是"纯业务逻辑、无 HTTP"。Agent 工具天然是同一层——编排业务服务，不碰路由。
2. **权限边界清晰**：adapter 只调既有 service/repository 函数（如 `risk_service.check_storyboard_risk`、`delivery_service.check_delivery`），**禁止直写 DB、禁止绕过 service**。路由层（未来 `api/v1/agent.py`）只做 HTTP + `Depends(require_work_access)` + 工具白名单分派。模型输出永远只是"提议"，执行权在后端（任务书 §2.3）。
3. **不污染网关**：`backend/ai/gateway.py` 是模型调用层，保持"只管调用"的纯粹性；工具注册/上下文装配是业务编排，放 services。
4. **可测试**：adapter 是纯函数/小类，可单元测试（合法/非法输入、权限拒绝、异常），与现有 `tests/` 风格一致。

备选（已否决）：放 `backend/ai/`——那是模型调用基础设施，放业务编排会混淆职责；放 `backend/api/v1/`——会把业务逻辑写进路由层，违背现有分层。

---

## 4. 任务执行模型

```
用户一句话 → brief 解析（gateway.generate_with_retry + Pydantic schema）
  → planner 生成结构化计划（JSON：steps=[{tool_id, params, depends_on}]，后端白名单校验）
  → 创建 AITask(task_type="agent_run", payload={brief, plan})，202 返回 task_id
  → queue 执行：每步 = 工具白名单分派 → adapter 执行 → 写 agent_steps
  → 必需 artifact 落盘（Work/WorkVersion/Shot/CopyDoc...）→ 保存后重读校验
  → 质量门禁（风险 flags/事实引用/预算/时长一致性）→ succeeded / partial / failed
前端：轮询 GET /api/v1/ai-tasks/{id}（复用现有 2.5s 轮询 + 惰性超时扫描），progress 真实更新
```

- **状态机**：复用 `TASK_STATUS_*` 五态；任务书要的 `waiting_user` / `partial` 暂用 `payload_json` 内的 `run_status` 扩展字段表达（`running+waiting_user` / `succeeded+partial`），**不改状态机表**——状态机变更是迁移风险，先验证再说。Phase 8 若证据表明不够，再提迁移。
- **取消**：复用 `cancel_requested` 协作式取消 + `POST /{task_id}/cancel`。
- **失败/重试**：步骤失败记 `agent_steps`，可重试单步；run 级重试走新 task（幂等键防重复）。
- **并发写保护**：同一作品的写操作串行化 + `Work.version` 乐观锁；冲突时失败并提示用户，不静默覆盖。

---

## 5. 权限边界

1. 工具调用**每次**从服务端 `CurrentUser` 取身份（`get_current_user`），不信任模型返回的 `org_id/user_id/work_id`。
2. 所有对象 ID 先由后端重读并确认归属（`require_work_access` / `require_org_member` 模式，404 防枚举）。
3. 工具白名单：只有注册在 `tools.py` 的工具可被分派；`planner` 输出的 `tool_id` 不在白名单 → 整步拒绝。
4. 分级执行：`user_confirmation_required` 级（删除作品、大范围覆盖、发布、高成本批量）必须先落 `waiting_user` 等确认，确认后才执行。确认态持久化在 `AITask.payload_json`，刷新不丢失。
5. 模型输出/上传内容视为不可信输入：只允许进 prompt 参数与业务字段，**禁止**进 shell/SQL/文件路径/URL（任务书硬约束，工具层 schema 做长度/枚举/大小限制）。

---

## 6. 幂等策略

| 场景 | 策略 | 现状 |
|---|---|---|
| 重复点击"生成" | `Idempotency-Key`（前端每次用户主动点击生成新 key）→ `core/idempotency.py` | 已有，内存版；Phase 7 补 DB 版 |
| Agent run 重复提交 | run 创建走 `POST /ai-tasks` 幂等（已有）；作品写回前检查 `agent_steps` 去重（`task_id+tool_id+params_hash` 唯一） | 部分已有，step 去重需新建 |
| 重复扣费 | 预算扣减在 gateway 成功后；幂等回放不重复调模型 → 不重复扣费 | 已有（单进程内） |
| 多 worker/重启 | 内存幂等丢失 → 可能重复执行 | **已知缺口**，Phase 7 用 DB 表补 |

---

## 7. 成本归属

- 归属键：`ai_calls.org_id`（组织）+ `ai_calls.task_id`（run）。Agent 每步 gateway 调用必须传 `task_id=<agent_run 的 AITask.id>`（`on_call_end` 的 `task_id` 参数已支持，`ai_observability.py:350`）。
- 预算/配额：走现有 `check_budget` + `check_quota`（402），Agent 服务端编排显式 `bind_org()`，不依赖前端请求头。
- 单 run 上限：复用网关熔断（5 次调用/500K token 是**单网关实例**计数；Agent 多步需在 planner 层另设 run 级上限：最大步数/最大模型调用数/费用上限，超限进 `partial` 并保留已有成果——任务书 §7.2）。
- 用户级限额（每天 5 次）：代码中缺失，工具层预留 `check_user_quota()` 桩位，Phase 7 对接另一 agent 的实现。

---

## 8. UI 约束（硬）落实方案

- **不动现有**：导航 v0–v6、暖纸白 `--paper #faf9f4`、田野深绿 `--field-deep #1d3a25`、麦金体系、`frontend/css/components.css` 全部保留；不引入新 CSS 框架。
- **新增入口**：侧边栏加一项 `AI 创作助手`（复用 `.nav-item` 样式与 SVG 图标规范），首页（v0）加「AI 一句话创作」主入口卡片（复用现有卡片/按钮规范）。
- **不替换工作台**：助手是新增视图（`data-v="v7"` 或独立路由），原 v1 工作台原样保留；各专业页面（产品资料/创意/分镜/风险/实拍）加轻量"让 AI 处理当前内容"按钮，传递真实 `product_id/work_id/shot_id`。
- 任务状态、执行计划、取消/重试/错误详情全部走真实 API（`GET /api/v1/ai-tasks/{id}`），不做固定延迟模拟。

---

## 9. Phase 2 入口条件（本阶段 Gate）

- [x] `TOOL_MAPPING.md` 落盘：8 逻辑工具 + 6 扩展工具，每行有真实文件/函数/行号证据
- [x] 复用/适配/缺失三类清单明确（本文件 §1）
- [x] 新增表最小化结论：只加 `conversations` / `conversation_messages` / `agent_steps`，复用 `AITask`/`Work`/`WorkVersion`（§2）
- [x] 工具层位置决策：`backend/services/agent/` + 理由（§3）
- [x] 任务执行模型/权限边界/幂等/成本归属明确（§4–§7）
- [x] UI 硬约束落实方案（§8）
- [ ] Phase 2 首要动作：逐路由审计权限 Depends（TOOL_MAPPING 中"未逐行核实"项），再进白名单

*— Phase 1 设计，2026-10-21。只读审计，未写业务代码。*
