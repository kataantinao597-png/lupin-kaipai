# 鲁品开拍后端架构（039：模块化单体）

## 模块划分

```
backend/
├── app.py                  # 入口：app 创建、中间件、路由挂载（薄）
├── core/                   # 基础设施（无业务逻辑）
│   ├── config.py           # 024：Pydantic Settings 统一配置
│   ├── deps.py             # 023：依赖注入（get_db/get_current_user/get_ai_service）
│   ├── exceptions.py       # 021：统一异常体系（LupinError + 错误码）
│   ├── logging.py          # 021：JSON 结构化日志（自动脱敏）
│   ├── request_id.py       # 请求 ID 中间件（并行组）
│   └── response.py         # 标准响应信封（并行组）
├── api/
│   └── v1/
│       ├── ideas.py        # 022：/api/ideas, /api/storyboard, /api/regenerate-shot
│       ├── risks.py        # 022：/api/risk-check, /api/delivery-check
│       ├── exports.py      # 022：/api/export, /api/shooting-checklist, /api/validate, /api/export-srt
│       ├── feasibility.py  # 022：/api/feasibility-check
│       ├── works.py        # Phase 2 新体系（/api/v1 前缀，租户隔离）
│       ├── orgs.py         # Phase 2 新体系
│       └── ai_tasks.py     # Phase 2 新体系
├── services/               # 021：业务逻辑层
│   ├── delivery_service.py # 交付检查（plan/final 两套标准）
│   └── risk_service.py     # 风险审核（AI 层 + 规则层）
├── domain/                 # 025：领域实体（与 ORM 分离）
│   ├── entities.py         # Work/Fact/Shot/Storyboard
│   └── converters.py       # ORM → 领域实体转换
├── repositories/           # 数据访问层（Phase 3）
│   ├── base.py             # 事务、乐观锁异常
│   ├── work_repository.py  # 025 示例：get_domain()/list_domain_by_org()
│   ├── fact_repository.py
│   ├── storyboard_repository.py
│   └── user_repository.py
├── models.py               # SQLAlchemy ORM（表结构映射）
├── auth/                   # 认证模块（Phase 2）
├── ai_service.py           # AI 调用封装（OpenAI 兼容）
├── prompts.py              # Prompt 模板
├── risk.py                 # 风险规则（确定性）
├── validator.py            # 分镜校验规则
└── exporter.py             # DOCX/SRT 导出
```

## 依赖方向（强制）

```
api → services → repositories → models
 │       │            │
 └───────┴────────────┴──→ domain (纯业务对象，无依赖)
         │
         └──→ core (config/deps/exceptions/logging，无业务逻辑)
```

**禁止反向依赖**：
- services 不得 import api
- repositories 不得 import services 或 api
- domain 不得 import 任何 backend 模块（除 Pydantic）
- core 不得 import 业务模块

`api → core/deps` 是唯一的横向依赖（依赖注入）。

## 分层职责

| 层 | 职责 | 不做 |
|---|---|---|
| api | 参数接收、Pydantic 校验、异常→HTTP 转换、返回 | 业务规则、SQL、AI 调用细节 |
| services | 业务规则编排、跨模块协调 | HTTP、SQL、路由 |
| repositories | 数据 CRUD、事务、乐观锁 | 业务规则、HTTP |
| domain | 业务实体 + 不变规则（如 Fact.is_usable） | 持久化、HTTP |
| core | 配置、依赖、异常、日志 | 业务逻辑 |

## 新增模块 Checklist

新增一个业务模块（如 `backend/api/v1/shooting.py`）时：

- [ ] 路由文件只做参数接收与异常转换，逻辑委托 services
- [ ] 业务规则写在 `backend/services/<name>_service.py`
- [ ] 需要持久化 → 走 repositories，不写裸 SQL
- [ ] 需要领域实体 → 用 `backend/domain/entities.py`，转换走 converters
- [ ] 依赖通过 `backend/core/deps.py` 注入（`Depends(get_...)`）
- [ ] 配置从 `get_settings()` 读，不直接 `os.environ`
- [ ] 异常抛 `core/exceptions.py` 的业务异常，不抛裸 Exception
- [ ] 在 `backend/app.py` 的 `_mount()` 注册路由
- [ ] 跑 `tests/test_db_phase3.py` 确认无回归

## 已知技术债

- `include_router` 在 FastAPI 0.143.2 有 bug，`app.py` 用 `_mount()` 手动挂载绕过
- 老 `/api/*` 路由仍抛 `HTTPException({"detail": ...})`，未统一为标准信封（026 TODO）
- `services/` 目前只有 delivery/risk；feasibility 合并逻辑仍在路由层（021 TODO）
- `domain/` 目前只有 4 个实体；Organization/Review/AITask 待补（025 TODO）
