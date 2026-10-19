# 《鲁品开拍》V3.0 阶段 A：真实代码审计报告

- 审计日期：2026-10-19
- 基线：V2.1 已验收（285 passed / 1 skipped / 0 failed，审计当日实测）
- 审计方式：只读。所有结论附真实文件/行号证据，无推测。

---

## 1. 当前架构与关键数据流

### 1.1 目录与入口

| 层 | 位置 | 说明 |
|---|---|---|
| 后端入口 | `backend/app.py` | FastAPI；路由用 `_mount()` 手动挂载（绕过 FastAPI 0.143.2 include_router bug），统一前缀 `/api/v1` |
| API 路由 | `backend/api/v1/*.py`（23 个模块） | works/ideas/risks/facts/assets/shoot_v2/timeline/colloquial/exports/ai_tasks/poster_image/reviews/... |
| 数据模型 | `backend/models.py` | 30 个表（Organization/Work/Storyboard/Shot/Fact/Asset/AITask/AICall/WorkVersion/...） |
| 迁移 | `migrations/versions/` | 当前 head `0010_fact_scope`（单 head，已修复过双 head） |
| 存储 | `backend/media/storage.py` | 抽象 StorageBackend；local（默认）/s3（LUPIN_STORAGE_BACKEND），key 格式 `{org_id}/{work_id}/{uuid}_{safe_name}` |
| AI 调用 | `backend/ai/gateway.py`（AIGateway） | provider=deepkey（中转），budget/限流/重试/观测齐全 |
| 前端 | `frontend/app.html` + `frontend/js/app.js`（2272 行单文件） | 7 视图 v0–v6；原生 JS，无框架 |
| 部署 | `Dockerfile` + `DEPLOY.md` | 腾讯云 CloudBase 云托管；密钥只走环境变量 |

### 1.2 关键数据流

```
用户输入（v1工作台/v2产品资料/v3创意策划）
  → Work（data_json 为主存储，含 storyboard/shoot_state/shot_acceptance/风险结果）
  → AI 生成（AIGateway → deepkey 中转；ai_calls 表记 tokens/cost/latency）
  → Fact 事实库（跨作品，租户隔离；status=confirmed 且 allow_use 才可被 AI 引用）
  → 拍摄执行（v5，shoot_v2 接口写 data_json 的 shoot_state/shot_acceptance）
  → 素材上传（upload_intents 签发 token → assets 表元数据 + 对象存储文件）
  → 交付导出（DOCX/SRT/TXT，前端把 data_json POST 给 /api/export 等接口）
```

**双轨存储注意**：分镜主数据在 `works.data_json`（页面渲染、导出、前端版本保护 localStorage 都走它）；
`storyboards/shots` 表只被 `timeline.py:94`（StoryboardRepository.list_by_work）读取做校验。两轨由谁写、何时同步，
代码里没有显式同步路径——timeline 校验可能读到空表。**导出/DOCX/页面三者一致（都源于 data_json），与 shots 表无关联。**

---

## 2. V2.1 已有能力 / 未完成能力

### 2.1 可靠运行（V3.0 严禁重实现）

| 能力 | 证据 | 状态 |
|---|---|---|
| 分镜状态机 | `shoot_v2.py:54` SHOOT_TRANSITIONS 六态 + `:64` ACCEPT_TRANSITIONS 三态；非法 400 | ✅ 已实现且测试通过 |
| 项目保存 | `works.py:281` 乐观锁 version，409→前端二选一（强制保存/放弃） | ✅ 已实现且测试通过 |
| 素材上传 | `assets.py:202` upload-intent→put；commit 失败清孤儿；文件丢失→410 | ✅ 已实现且测试通过 |
| 人工验收 | `shoot_v2.py` accept_shot；验收数据存 `data_json.shot_acceptance` | ✅ 已实现且测试通过 |
| DOCX 导出 | `shoot_v2.py:447` edit-list.docx + `exports.py` 拍摄执行单/SRT | ✅ 已实现且测试通过 |
| 错误码/幂等 | `core/error_codes.py` 统一业务码；`core/idempotency.py` Idempotency-Key 24h 去重 | ✅ 已实现且测试通过 |
| AI 观测 | `ai_observability.py`：tokens/cost_cents/latency_ms 后台记 ai_calls | ✅ 已实现且测试通过 |
| Fact 状态机 | `facts/state_machine.py` 六态 draft→pending_review→confirmed→expired/revoked（+rejected）；流转写 FactStatusHistory | ✅ 已实现且测试通过 |

### 2.2 未完成 / 部分（影响 V3.0 的单独列出）

| # | 事项 | 证据 | 影响 |
|---|---|---|---|
| A | **无 Product 实体表** | `models.py` 无 products 表；`Fact.product_id` 是自由字符串 | V3.0 产品知识中心（§4）必须新建产品档案模型 |
| B | 无跨项目素材引用关系 | `Asset.work_id` FK CASCADE；删除直接删文件（`assets.py:310-329`）；无引用计数/授权字段 | V3.0 素材库（§9）必须新建引用表，删除逻辑要区分引用与实体 |
| C | 无文案版本管理 | 只有 WorkVersion（作品整包版本）；colloquial 改写不存版本历史 | V3.0 文案优化器（§6）必须新建文案版本模型 |
| D | 无发布/复盘模型 | 无 publish/effect 表；店播脚本只有 prompt 无 API 路由（`livestream_v1.py` 存在但 `backend/api/` 无路由） | V3.0 发布中心（§8）/效果复盘（§10）从零建 |
| E | 无自动保存 | 前端无 debounce 自动保存；只有手动 saveWork | 并发保护靠 409+幂等，够用；自动保存可暂缓 |
| F | timeline 双轨 | `timeline.py` 读 storyboards/shots 表，主数据在 data_json | 新增模块一律以 data_json 为准，不要往 shots 表写新逻辑 |
| G | 幂等多 worker 限制 | `idempotency.py` 进程内存存储，重启/多 worker 后退化 | 生产多实例部署需升级到 DB/Redis（P1） |
| H | AI 依赖不可验证 | 本审计环境无 LUPIN_API_KEY（config.py:83 无 key→AI 接口 503） | 所有 AI 生成类功能标记"因外部依赖无法验证"，用 mock 只验业务逻辑 |

---

## 3. V3.0 新增能力与复用能力边界

### 3.1 可直接复用的（不许重造）

- **Fact 事实库**：表/状态机/审计历史/版本（FactVersion）/导入接口（fact_import.py）全套已实现。产品知识中心直接扩展它，不许另建事实表。
- **风险检查**：`risk.py` rule_scan + model_review + check_fact_grounding；RiskFlag 已含 shot_number/linked_text/suggestion/status。文案优化器直接调。
- **AI 调用封装**：AIGateway（预算/限流/重试/超时 TOTAL_TIMEOUT/观测/幂等头）。新 AI 功能一律走它。
- **权限体系**：`rbac.py` require_org_permission / require_work_access；资产权限 `asset:read/write/delete`。新接口必须挂后端权限检查，不许只靠前端隐藏按钮。
- **存储抽象**：StorageBackend（local/s3）。素材库复用，不许另起上传系统。
- **DOCX/SRT 导出**：exporter.py + shoot_v2 edit-list。复盘导出可复用其模式。

### 3.2 必须新增的（V3.0 §4–§10）

| V3.0 模块 | 现状 | 复用点 |
|---|---|---|
| §4 产品知识中心 | 无 Product 表；产品信息散在各 work.data_json | Fact 表（加 product 关联）、facts API |
| §5 内容策划工作台 | ideas API（创意生成）有；无"营销目标→创意→关联拍摄项目"流程 | ideas.py、feasibility_v2 |
| §6 文案优化器 | colloquial 改写有（单次，无版本）；无优化目标/版本/局部重写 | colloquial.py、AIGateway、risk.py |
| §7 多平台适配 | 尚未实现 | 文案版本模型 |
| §8 发布中心 | 尚未实现（只有 work.status=archived） | Work 状态、Asset |
| §9 跨项目素材库 | 无引用/授权/检索 | Asset 表 + storage 抽象 |
| §10 效果复盘 | 尚未实现 | 发布记录 |

### 3.3 V2.1 → V3.0 的数据纽带（已存在，可直接用）

- `Fact.product_id`（String(64)）：产品档案建表后，现有事实可通过该字符串回填关联。
- `data_json.shot_acceptance / shoot_state`：发布中心可直接引用"已验收镜头"。
- `Asset.org_id/work_id` + key：素材库跨项目检索的天然维度。
- `ai_calls`：成本归因到具体生成任务。

---

## 4. 实际需要修改的文件、接口、数据结构

> 阶段 B 只列 B1–B4；C–H 的在各自阶段审计时细化。

**B1. 产品档案表（新建）**：`models.py` 加 `class Product`（id/org_id/名称/品类/产地/品牌资料/品牌语气/卖点/宣传限制/版本/时间戳）+ migration 0011；`repositories/product_repository.py` 新建；`api/v1/products.py` 新建 CRUD（挂 require_org_permission）。

**B2. 事实→产品关联**：`Fact.product_id` 现为自由字符串；B1 落地后加外键/回填脚本（历史数据兼容：空 product_id 的事实保持可查）。

**B3. 产品资料 v2 视图**：`frontend/js/app.js` + `app.html` 在现有 facts 管理旁加产品档案选择器；新建内容任务时可选产品档案（减少重复录入）。

**B4. 产品资料变更影响面**：事实/分镜引用了旧版本产品信息时，只做"引用旧版本"标记，**不静默改写历史文案/已确认分镜**（任务书 §4.3 铁律）。

---

## 5. 数据兼容及安全风险

1. **历史数据兼容**：`Fact.product_id` 存量为自由字符串，B2 回填必须容忍空值/孤儿引用；`data_json` 里散落的产品信息不做迁移，只做"新建档案时可选导入"。
2. **租户隔离**：所有新表必须有 `org_id` + 索引；新接口必须 `Depends(require_org_permission)`；素材库跨项目检索**只能看同 org**，私有项目素材默认不可见（除非显式授权字段）。
3. **删除语义**：Asset 删除当前是"删文件+置 deleted"。素材库引入引用关系后，删除必须先查引用计数：有引用→只删引用关系；无引用→二次确认后删实体。**现在不改**，B 阶段只加模型不改删除逻辑，避免 regression。
4. **AI 成本**：新 AI 功能一律走 AIGateway（已有预算/超时/幂等），不许直调 provider。
5. **密钥**：LUPIN_API_KEY 只走环境变量；审计环境未配置是预期状态，不代表生产不可用。
6. **日志**：`gateway.py:164` 只记 prompt_version 不记内容——新功能保持此纪律。

---

## 6. 可复现的基线测试结果

```
cd ~/workspace/lupin-kaipai && ./venv/bin/python -m pytest -q
285 passed, 1 skipped, 157 warnings in 66.60s
```

- 命令：项目 `./venv/bin/python -m pytest`（testpaths=tests）
- 29 个测试文件；1 skipped 为预期的外部依赖跳过
- 前端：`node --check frontend/js/app.js` 通过

---

## 7. 能力标记总表

| 能力 | 标记 |
|---|---|
| 分镜状态机（六态+三态） | 已实现且测试通过 |
| 项目保存/409 冲突/版本恢复 | 已实现且测试通过 |
| 素材上传/签名下载/孤儿清理/410 | 已实现且测试通过 |
| 人工验收/补拍清单/剪辑清单 | 已实现且测试通过 |
| DOCX/SRT/TXT 导出 | 已实现且测试通过 |
| Fact 事实库（六态/审计/版本/scope） | 已实现且测试通过 |
| 风险检查（规则+模型+grounding） | 已实现且测试通过 |
| 幂等（Idempotency-Key 24h） | 已实现且测试通过（多 worker 限制见 §2.2-G） |
| AI 网关（预算/限流/重试/观测） | 已实现且测试通过 |
| 自动保存 | 尚未实现 |
| 产品档案（跨项目复用） | 尚未实现 |
| 内容策划流程（目标→创意→关联拍摄） | 部分实现（ideas 生成有，流程无） |
| 文案优化器（版本/局部重写/恢复） | 部分实现（colloquial 单次改写有） |
| 多平台适配 | 尚未实现 |
| 发布中心 | 尚未实现 |
| 跨项目素材库 | 尚未实现 |
| 效果复盘 | 尚未实现 |
| 店播场控脚本 API | 尚未实现（只有 prompt） |
| 真实 AI 模型调用 | 因外部依赖无法验证（无 key，接口 503 降级已实现） |
| 真实 S3 存储 | 因外部依赖无法验证（默认 local） |

---

## 8. 阶段 B（产品知识中心）最小可独立交付清单

按低风险优先排序，每步独立可验收：

1. **B-1（最低风险）**：新建 `Product` 表 + migration 0011 + repository + CRUD API（只读/写产品档案，不碰现有表）。验收：pytest 新增 ≥6 用例；旧测试全绿。
2. **B-2**：facts API 支持按 product_id 筛选/关联（`GET /api/v1/facts?product_id=` 已有 product_id 字段，只加查询参数）；存量自由字符串兼容。验收：旧事实可查、新建事实可挂产品。
3. **B-3**：前端 v2 视图加产品档案选择器（app.js/app.html 增量，不改现有 facts 逻辑）。验收：Firefox 冒烟 0 JS 报错。
4. **B-4**：新建内容任务时可选产品档案（ideas/storyboard 请求体加可选 product_id，后端把该产品的 confirmed 事实自动带入 grounding 上下文）。验收：复用 check_fact_grounding；mock 测试。
5. **B-5**：产品资料变更→引用旧版本的事实/分镜打"版本过期"标记（只标记不改写）。验收：标记逻辑单元测试。

**明确不做的**：不迁移 data_json 里散落的产品信息；不改 Asset 删除逻辑；不建文案/发布/复盘表（那是 C–H 的事）。
