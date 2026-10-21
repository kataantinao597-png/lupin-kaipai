# TOOL_MAPPING.md — 现有功能 → Agent 工具复用映射

> Phase 1 §1.2 产物。分支 `feat/agent-workspace-commercial`，只读审计，未改业务代码。
> 证据规则：每个映射给出真实文件 + 函数/路由 + 行号；未逐行核实的项如实标注"未逐行核实"，
> 完全不存在的标"缺失"。行号为审计时的行号，后续代码变动可能漂移。

## 0. 底层 AI 能力（所有工具共享的调用底座）

| 能力 | 真实实现位置 | 说明 |
|---|---|---|
| 统一 AI 入口 | `backend/ai/gateway.py:67` `AIGateway` | `chat()`（258 行）/ `generate_with_retry()`（303 行）。所有业务（创意/分镜/风险/可行性）都走网关；`ai_service.AIService` 仅为传输层 |
| OpenAI 兼容协议 | `backend/ai/providers/deepkey_adapter.py:15` `DeepKeyAdapter` | "deepkey（OpenAI 兼容 /chat/completions）适配器"；当前唯一的生产 provider。能力声明：`structured_output=True`，`streaming=False`，**无 function/tool calling 能力声明**（见 `capabilities()` 23-33 行） |
| 结构化 JSON 输出 | `backend/ai/gateway.py:303` `generate_with_retry` | `json_object=True` + `validate_fn` 校验，失败带反馈重试（最多 4 次，`TOTAL_TIMEOUT=600s` 见 `backend/ai_service.py:15`） |
| 超时/重试 | `backend/ai/retry.py:97` `RetryExecutor` + `backend/ai/retry.py:91` `backoff_seconds` | 按错误分类（timeout/connection/rate_limit 等）的有限重试 + 指数退避；429/5xx/timeout 埋点进 `backend/core/metrics.py` |
| 模型选型策略 | `backend/ai/model_policy.py` `resolve_model` / `resolve_fallback` | `model=None` 时按 `task_type` 查 `TaskModelPolicy` 表；主模型失败自动 fallback 一次（`gateway.py:130` 逻辑），记 failover 台账 |
| token/成本台账 | `backend/models.py:636` `AICall`（表 `ai_calls`） | 每次模型调用一条记录：`task_id/org_id/task_type/model/prompt_tokens/completion_tokens/total_tokens/cost_cents/status/latency_ms/cached`；`cached` = 缓存命中未真实调用 |
| 成本估算 | `backend/models.py:671` `ModelPrice` + `backend/ai_observability.py:252` `estimate_cost_cents` | 按每 1k token 输入/输出价格估算，`ai_calls.cost_cents` |
| 可观测性 | `backend/ai_observability.py` | `on_call_start/on_call_end`（314/350 行）：并发信号量、断路器（`breaker_check` 47 行）、内存缓存（`cache_get/cache_put` 154/166 行）、后台写线程 `record_call_async`（302 行，best-effort，进程重启可能丢尾部记录） |
| 组织预算 | `backend/ai/budget.py:42` `check_budget` / `:59` `charge_usage` | 组织月度 cents 预算；超限抛 `BudgetExceededError` → API 层 402。由 `gateway._check_budget`（`gateway.py:184`）在每次调用前检查 |
| 组织配额 | `backend/services/quota_service.py:97` `check_quota` | 配额类型 `ai_calls_daily` / `storage_mb` / `members`（29-33 行）；**仅组织维度**，无记录=不限；超限抛 `QuotaExceededError`（`BudgetExceededError` 子类）→ 402。同样由 `gateway._check_budget` 触发 |
| ⚠️ 预算生效前提 | `backend/core/deps.py:120` `get_ai_gateway` | `org_id` 只从 `X-Org-Id` 头或 `?org_id=` 取；**两者都没有 → `org_id=None` → 预算/配额检查静默跳过**（代码注释 135-137 行明确说明）。同步接口若前端未声明 org，表现为"预算已配置但实际未记账"。异步任务由 runner 注入 `_org_id` + `bind_org()` 对齐（`ai_tasks.py:126` 注释 S6.1，`gateway.py:98` `bind_org`） |
| 幂等 | `backend/core/idempotency.py:123` `idempotency_begin` | `Idempotency-Key` 头：同 key 同体回放（TTL 24h）、同 key 异体 400、并发双击等待首个完成。**进程内内存存储**（上限 1000 条）：多 worker 进程之间、进程重启后记录丢失，退化为"各自执行一次"（文件头 13-17 行已知局限） |
| AI 异步任务 | `backend/models.py:566` `AITask`（表 `ai_tasks`） | 状态机 `pending→running→succeeded/failed/cancelled`，流转必须走 `backend/ai_tasks/state_machine.py:44` `validate_task_transition()`（非法抛 `IllegalTaskTransition`）；`progress` 0-100；`cancel_requested` 协作式取消；`priority` high/low |
| 异步任务 API | `backend/api/v1/ai_tasks.py` | `POST /api/v1/ai-tasks`（100 行，202 + 幂等 + 强制登录 + org 成员校验）；`GET /{task_id}`（155 行，轮询，惰性超时扫描）；`POST /{task_id}/cancel`（179 行）；`GET /ai-tasks` 列表（206 行） |
| 异步执行器 | `backend/ai_tasks/runners.py:387` `RUNNERS` + `backend/ai_tasks/queue.py` | `ThreadPoolExecutor` 优先级队列；`RUNNERS` 目前只有 4 个：`ideas / storyboard / livestream_script / livestream_regen_segment`。**`AI_TASK_TYPES`（`models.py:740`）中还有 `regen_shot / feasibility / risk_check / colloquial_rewrite`，但没有 runner → 这些类型的 `?async=true` 会 400 "暂不支持异步"** |
| 进程重启恢复 | `backend/ai_tasks/queue.py:33` `sweep_timed_out` | 惰性扫描：pending 超时→cancelled，running 超时→failed。**恢复语义 = 标记为可解释的失败，不续跑** |
| RBAC | `backend/rbac.py` | 5 角色 owner>admin>editor>reviewer>viewer；`require_org_permission(action)`（139 行）、`require_work_access(action)`（162 行，防 IDOR，404 防枚举）、`ORG_ADMIN_ONLY_ACTIONS` 硬限制（85 行）、`assert_can`（189 行，handler 内二次校验）。权限点覆盖 org/work/fact/product/copy/plan/publish/review/asset |

---

## 1. 逻辑工具映射表（任务书 §1.2）

> "权限检查"列：已确认 ai-tasks 路由强制登录+组织成员（V3.1 P0）；其余路由的 Depends 未逐行审计，
> 标注"未逐行核实"——Phase 2 必须逐路由审计后再接入 Agent 白名单。

| 逻辑工具 | 现有实现位置（文件:行:函数/路由） | 输入/输出（真实 schema） | 权限检查 | 是否已有测试 | 处理方式 |
|---|---|---|---|---|---|
| 产品事实查询 | `backend/api/v1/facts.py:36`（prefix `/facts`）：`POST /`（77 行）、`GET /`（97 行）、`GET /{fact_id}`（121 行）、`POST /{fact_id}/transition`（137 行，状态机）、`GET /{fact_id}/history`（180 行）、`PATCH /{fact_id}/content`（203 行）、`GET /{fact_id}/versions`（231 行）；`backend/api/v1/products.py:37`（prefix `/products`）：CRUD（129/157/169/185/216 行）；`backend/api/v1/fact_import.py`：`POST /import-document` | 输入：`FactOut`（`api/v1/schemas.py:291`）、`ProductOut`（`api/v1/schemas.py:384`）；事实有状态/版本/历史模型 `Fact/FactStatusHistory/FactVersion`（`models.py:190/224/248`） | 未逐行核实（路由文件内应有 org 成员校验，需 Phase 2 审计） | 有：`tests/test_fact_lifecycle.py`、`tests/test_fact_import.py`、`tests/test_v30_product.py` | **直接复用**。Agent 工具 `query_facts` / `query_products` 直接调这些路由或其 repository；事实"已确认/待核实"状态机现成可用 |
| 营销策划/创意 | `backend/api/v1/ideas.py:223` `POST /api/ideas`（同步；`?async=true` 经 `_enqueue_ai_task` 155-221 行转异步任务）；`backend/api/v1/plan.py:294` `POST /plan/tasks/{task_id}/generate`、`365` `/select`、`382` `/feasibility`（内容策划工作台 V3.0 C）；异步 runner `run_ideas`（`ai_tasks/runners.py:88`） | 输入：`IdeasRequest`（`ideas.py:64`）；输出经 `gateway.generate_with_retry` + `_validate_ideas`（`ideas.py:123`）校验的 JSON | `create_ideas` 用 `_optional_user` + `get_ai_gateway`（未逐行核实 org 绑定是否强制）；plan 路由未逐行核实 | 有：`tests/test_async_tasks_phase4.py`、`tests/test_v30_plan.py` | **适配**。创意生成逻辑可直接复用，但 Agent 需要"策略→创意方向→选择"的多步编排，现有接口是单次调用，需在工具层做参数装配与结果落盘（写回 Work/WorkVersion） |
| 分镜生成/局部重生成 | `backend/api/v1/ideas.py:290` `POST /api/storyboard`、`367` `POST /api/regenerate-shot`（单镜头重生成）；`backend/ai_tasks/runners.py:126` `run_storyboard`（异步）；`backend/api/v1/shoot_v2.py:228` `PATCH /api/v2/shoot/works/{work_id}/shots/accept`（镜头确认）；模型 `Storyboard`/`Shot`（`models.py:121/138`），`Shot.locked`（`models.py:156-158`，"锁定镜头不许改动"） | 输入：`IdeasRequest` 复用；输出分镜 JSON 经 `_validate_shot`（`ideas.py:141`）校验 | 未逐行核实 | 有：`tests/test_async_tasks_phase4.py`、`tests/test_shoot_v2.py`、`tests/test_regression.py`（分镜相关） | **适配**。"锁定镜头保护"机制已存在（`locked` 字段 + `set_shot_lock`），Agent 的局部重生成工具直接复用 `regenerate-shot` 并在工具层强制检查 `locked`；分镜版本管理（`storyboards.version` 乐观锁）可直接用 |
| 风险/可行性 | `backend/api/v1/risks.py:47` `POST /api/risk-check` → `backend/services/risk_service.py:17` `check_storyboard_risk`（AI 识别 + V5+ 事实 grounding 规则层，确定性不耗 API）；`backend/api/v1/feasibility.py:72` `POST /api/feasibility-check`、`41` `POST /api/v2/feasibility-check`（`FeasibilityV2Request`）；`backend/api/v1/risks.py:80` `POST /api/delivery-check` → `backend/services/delivery_service.py:19` `check_delivery`（确定性交付检查） | 输入：`DeliveryCheckRequest`（`risks.py:37`）、`FeasibilityV2Request`（`feasibility.py:34`）；输出含 `flags` 列表（风险项，`risk_service.py:17-60`） | 未逐行核实 | 有：`tests/test_risk.py`、`tests/test_feasibility_v2.py`、`tests/test_feasibility_fallback.py`、`tests/test_delivery_check.py`、`tests/test_v21_risk_tolerance.py` | **直接复用**。风险检查已是"AI + 规则层"双通道，事实 grounding 可直接服务于"高风险事实必须引用/标记待核实"门禁 |
| 实拍指导/拍摄单 | `backend/api/v1/shoot_v2.py`：`PATCH /api/v2/shoot/works/{work_id}/shots/accept`（228 行）、`GET .../reshoot-list`（335 行）、`GET .../edit-list`（436 行）、`GET .../edit-list.docx`（531 行，DOCX 导出）；`backend/api/v1/exports.py:58` `POST /api/shooting-checklist`（`ChecklistRequest`） | 输入：`ChecklistRequest`（`exports.py:31`）；输出拍摄清单/补拍清单/DOCX 文件 | 未逐行核实（shoot_v2 用 `require_work_access` 的可能性高，需 Phase 2 确认） | 有：`tests/test_shoot_v2.py` | **直接复用**。拍摄执行单/补拍清单/剪辑单逻辑完整，Agent 只需按 brief 参数调用并落盘 |
| 文案/平台内容 | `backend/api/v1/copyopt.py:54`（prefix `/copy`）：`POST /docs`（250 行，`CopyDocCreate`）、`POST /docs/{doc_id}/optimize`（358 行）、`POST /docs/{doc_id}/rewrite-paragraph`（547 行）、版本 confirm/restore（486/510 行）；`backend/api/v1/platforms.py:55`（prefix `/platforms`）：`POST /copy/docs/{doc_id}/platforms`（201 行）、`GET ...`（327 行）、`/{pv_id}` CRUD + export（343-401 行）；模型 `CopyDoc/CopyVersion/PlatformVersion`（`models.py:1199/1224/1149`） | 输入：`CopyDocCreate`（`copyopt.py:71`）；输出优化后文案版本（版本化存储，可恢复） | `_require_copy_perm`（`copyopt.py:101`）、`_require_publish_perm`（`publish.py:49`）存在，未逐行核实全覆盖 | 有：`tests/test_v30_copyopt.py`、`tests/test_v30_platform.py`、`tests/test_colloquial.py` | **直接复用**。文案版本化（confirm/restore）天然支持"多轮对话修改 + 版本恢复" |
| 素材/海报 | 素材库：`backend/api/v1/asset_library.py:147` `GET /search`、`222` `POST /refs`、`287` `GET /works/{work_id}/refs`；资产：`backend/api/v1/assets.py`：`POST /upload-intents`（149 行）、`POST /upload`（202 行）、`GET /`（280 行）、`DELETE /{asset_id}`（310 行）；模型 `Asset/AssetRef/UploadIntent`（`models.py:982/1015/1048`）。海报：`backend/api/v1/poster_image.py:119` `POST /poster/image`（`ImageGenOut`，`poster_image.py:61`），经 `_call_image_api`（67 行）直接调图模型 | 输入/输出：`AssetOut`、`UploadIntentOut`、`ImageGenOut`；素材与作品通过 `AssetRef` 关联 | 未逐行核实 | 有：`tests/test_v30_asset_library.py`、`tests/test_assets_consistency.py`、`tests/test_poster_image.py`、`tests/test_media_phase11.py` | **适配**。素材查询/关联可直接复用；海报生图**未核实**是否接入 `ai_calls` 成本台账与预算（`poster_image.py` 走独立 `_call_image_api`，未见 gateway 调用——需 Phase 2 核实，缺失则补缺） |
| 交付/导出/版本 | 作品版本：`backend/api/v1/works.py`：`GET /{work_id}/versions`（451 行）、`POST /{work_id}/versions/{version_no}/restore`（478 行）、`GET /{work_id}/export-json`（506 行）、`POST /{work_id}/export-all`（562 行）；导出：`backend/api/v1/exports.py`：`POST /api/export`（44 行）、`POST /api/export-srt`（81 行）、`POST /api/validate`（73 行）；发布：`backend/api/v1/publish.py:96` `run_prepublish_check`、`300` `POST /records`；审核：`backend/api/v1/review.py`（suggest/decision/dashboard）；交付门禁：`backend/services/delivery_gate_service.py`（`DeliveryGateConfig/Waiver`，`models.py:433/464`） | 输入/输出：`WorkOut`（`api/v1/schemas.py:162`）、`WorkVersionOut`（177 行）；审核状态机 `REVIEW_STATUS_*` + `REVIEW_TRANSITIONS`（`models.py:386-384`，内容变更自动 expired） | works 路由用 `require_work_access`（`rbac.py:162`，已确认存在该机制；各路由是否全部挂载未逐行核实） | 有：`tests/test_export.py`、`tests/test_srt_export.py`、`tests/test_v30_publish.py`、`tests/test_v30_review.py`、`tests/test_review_closure.py` | **直接复用**。版本快照/恢复、导出、发布前检查、交付门禁（含豁免 Waiver）完整；Agent 的"保存→重读校验→交付摘要"闭环可直接建立在这些 API 上 |

---

## 2. 扩展工具映射（任务书 §2.1 十个核心工具的其余项）

| 逻辑工具 | 现有实现位置 | 输入/输出 | 权限检查 | 是否已有测试 | 处理方式 |
|---|---|---|---|---|---|
| 对话理解/brief 解析 | **缺失**。无 agent 模块（`backend/` 下 grep "agent" 仅命中 webhook_service/ai_service 注释性文字）；无 Conversation/Run/Step 表 | — | — | 无 | **补缺**。新增 brief 解析 prompt + Pydantic schema（任务书 §5.1 字段），走 `gateway.generate_with_retry` 做结构化抽取 |
| 作品创建/读取/局部更新 | `backend/api/v1/works.py`：`POST /works`（140 行）、`GET /{work_id}`（258 行）、`PATCH /{work_id}`（285 行，乐观锁 `version`，`models.py:94-119`）、`DELETE`（354 行，软删除）、`POST /{work_id}/restore`（420 行）；版本快照 `_snapshot_work`（101 行） | `WorkOut` / `WorkVersionOut` | `require_work_access` 机制存在，未逐行核实 | 有（`tests/test_regression.py` 等覆盖 works 基础流程） | **适配**。Agent 的"局部更新"需在工具层实现 patch 语义（只写目标字段 + 版本号校验），不能直接透传全量 `data_json` 覆盖 |
| 任务状态查询/取消/重试 | `backend/api/v1/ai_tasks.py`（见 §0）；状态机 `backend/ai_tasks/state_machine.py:44` | `AITaskOut`（`api/v1/schemas.py:239`） | 已确认：强制登录 + 租户隔离（`get_task` 155-177 行，`_require_org_member`） | 有：`tests/test_async_tasks_phase4.py`、`tests/test_v21_idempotency.py` | **直接复用**。Agent run 可直接建模为一种 `task_type`，复用 202/轮询/取消/超时全套 |
| 成本/用量/预算查询 | `backend/api/v1/ai_stats.py`：`GET /calls`（路由清单见 grep，`/calls`）、`GET /dashboard`；`backend/services/ai_ledger.py:33` `org_calls_query`；`backend/api/v1/ai_config.py:139` `GET /budgets/{org_id}`、`157` `PUT`、`172` `POST /reset`；`backend/services/billing_service.py:142` `build_statement` | 未细读各 schema | 未逐行核实 | 有：`tests/test_ai_observability.py`、`tests/test_enterprise_phase12.py`（部分） | **适配**。用量口径已统一（`org_calls_query` 优先 `ai_calls.org_id`）；Agent 需在 run 维度汇总成本（按 `task_id` 过滤 `ai_calls`），属小适配 |
| 口播/直播脚本 | `backend/api/v1/ideas.py`（`livestream_script` 类型）；runner `run_livestream_script` / `run_livestream_regen_segment`（`runners.py:268/327`）；`backend/api/v1/colloquial.py:55` `POST /api/colloquial-rewrite`（拉呱改写）；`AI_TASK_TYPES` 含 `livestream_script/livestream_regen_segment/colloquial_rewrite` | `IdeasRequest` 复用；`_validate_livestream`（`runners.py:173`）、`_validate_colloquial`（`colloquial.py:39`） | 未逐行核实 | 有：`tests/test_livestream.py`、`tests/test_colloquial.py` | **直接复用**。`colloquial_rewrite` 契合"山东本土拉呱风"文案需求 |
| 时间线/执行计划展示 | `backend/api/v1/timeline.py:69` `GET /works/{work_id}/timeline`（`TimelineOut`） | `TimelineOut` | 未逐行核实 | 未核实 | **适配**。Agent 执行计划的前端展示可复用时间线思路，但 Agent step 的"计划→执行中→完成"状态是新增概念，需适配 |

---

## 3. 明确缺失项（诚实清单）

1. **Agent 工具注册表/白名单**：`backend/services/agent/` 不存在；无 `tool_id`/`schema_version`、无参数 schema 校验层。→ Phase 2 新建。
2. **对话持久化**：无 Conversation/消息表。`AITask.payload_json/result_json` 可暂存单轮，但多轮上下文、artifact 引用无处可放。→ Phase 3 最小新增（见 PHASE1_DESIGN.md §3）。
3. **原生 tool calling**：DeepKey 适配器能力声明无 function calling（`deepkey_adapter.py:23-33`）；任务书 §2.3 允许的"结构化 JSON 计划 + 后端白名单分派"是可行且更稳的路线。→ 设计决策，不补 provider 能力。
4. **用户级每日限额**（普通用户每天 5 次）：代码中不存在。`quota_service` 只有组织维度三种配额。用户 2026-10-21 已定方案并交由其另一 agent 实现，本分支未见实现——Agent 工具层设计时必须预留"用户级配额"检查点，不能假设它已存在。
5. **工具分级与确认机制**（`core_required` / `optional` / `user_confirmation_required`）：不存在。破坏性操作（删除作品、大范围覆盖）目前靠 `assert_can` + 前端确认，无统一的"需用户确认工具" gate。→ Phase 2 补缺。
6. **`regen_shot / feasibility / risk_check / colloquial_rewrite` 的异步 runner**：`AI_TASK_TYPES` 有类型但 `RUNNERS` 无实现，`?async=true` 会 400。Agent 长流程若需要这些任务的异步执行，需补 runner 或全部走同步（同步有 600s 总超时）。
7. **海报生图的成本归属**：`poster_image.py` 独立 `_call_image_api`，未核实是否写 `ai_calls` / 走预算。→ Phase 2 核实后决定补缺。
8. **DB 级幂等键**：`core/idempotency.py` 是进程内存实现；多 worker / 重启后失效。Agent 的"重复提交不重复建作品/不重复扣费"（任务书硬性指标）在单进程外无保障。→ Phase 7 补缺（幂等键表）。

---

## 4. 资产地图总览（一页速查）

```
前端  frontend/app.html（导航 v0首页/v1工作台/v2产品资料/v3创意策划/v4海报制作/v5拍摄执行/v6交付中心/模型配置）
      frontend/js/app.js + js/services/{api-client,auth-client,op-queue,request-guard}.js
      CSS 变量：--paper #faf9f4 / --field-deep #1d3a25 / --field #2e6b3e（frontend/style.css:5-14）

API   backend/api/v1/ 30 个路由模块（ideas/risks/feasibility/shoot_v2/exports/colloquial/copyopt/platforms/
      poster_image/assets/asset_library/facts/products/fact_import/publish/review/plan/works/ai_tasks/
      ai_models/ai_config/ai_stats/timeline/metrics/audit/orgs/security/enterprise/api_keys/batch）

AI    backend/ai/gateway.py（AIGateway：预算/重试/上限/配置记录/可观测）
      backend/ai/{budget,retry,config,capabilities,model_policy}.py + providers/deepkey_adapter.py（OpenAI 兼容）
      backend/ai_service.py（传输层，TOTAL_TIMEOUT=600）| backend/ai_tasks/{queue,repository,runners,state_machine}.py
      backend/ai_observability.py（断路器/并发/缓存/ai_calls 台账）| backend/services/ai_ledger.py（用量口径）

业务  backend/services/{quota,billing,delivery,delivery_gate,risk,review,webhook}_service.py
      backend/{risk,feasibility_v2,exporter,validator,prompts}.py | backend/rbac.py（5 角色矩阵）
      backend/models.py（45 张表：Work/WorkVersion/Storyboard/Shot/Fact/Product/AITask/AICall/
      AIBudget/Quota/CopyDoc/PlatformVersion/Asset/PublishRecord/Review/DeliveryGateConfig/Waiver...）

数据  AITask 状态机 pending/running/succeeded/failed/cancelled（validate_task_transition 强制）
      Work 乐观锁 version + 软删除 | Shot.locked 镜头锁定 | Review 内容变更自动 expired
```

*— Phase 1 审计，2026-10-21。未连 DB、未写业务代码、未跑测试。*
