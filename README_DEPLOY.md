# 鲁品开拍 V3.1 · 部署包说明

版本：V3.1（店播场控 / 拉呱版改写 / 展示页升级）｜ 打包日期：2026-10-19 ｜ 代码版本：见包内 `VERSION.txt`

## 包内容

- `backend/`：FastAPI 后端（含 V3.0 全部 8 个模块 + V3.1 安全与体验升级）
- `frontend/`：前端静态页（app.html、工作台；landing.html 展示页，SEO/OG 已补）
- `migrations/` + `alembic.ini`：数据库迁移（head = 0018_review；V3.1 无新增 migration）
- `scripts/`：运维脚本（`expire_facts.py` 建议挂 cron 每天跑一次，自动过期事实）
- `requirements.txt`、`Dockerfile`、`Dockerfile.prod`
- `DEPLOY.md`：原有部署文档（CloudBase 三种方式）

已剔除：venv / __pycache__ / .git / tests / e2e / docs / demo / **测试库残留**（V3.0 包误带了 14M 测试 sqlite，本包已剔除，生产按空库启动）。

## V3.1 主要变更（部署相关）

1. **AI 生成接口强制登录**：`POST /api/v1/ai-tasks`、`POST /api/colloquial-rewrite` 未登录返回 401。部署后观察 401 日志，确认无合法匿名调用被误伤。
2. **预算正式生效**：网关按组织记账，`LUPIN_BUDGET_*` 阈值请按商家套餐在控制台配置（验收时未核实具体数值）。
3. **幂等**：建任务支持 `Idempotency-Key` 头；注意幂等存储为进程内存——**单 worker 部署无影响，多 worker 需改 Redis**，否则跨进程重复提交可能重复扣费。
4. **其余 V3.0 生产适配仍在**：网关剥离 Authorization 时回退 `lupin_refresh` Cookie；Cookie path `/`；`/api/health` 返回 `ai_key_fp` 指纹。

## 环境变量（部署时在控制台配置，不要写进代码）

| 变量 | 说明 |
|---|---|
| `LUPIN_API_BASE` | `https://deepkey.top/v1` |
| `LUPIN_API_KEY` | deepkey 的 key（真实值只填在控制台） |
| `LUPIN_MODEL` | `gpt-6-luna` |
| `LUPIN_JWT_SECRET` | 生产务必配置随机长字符串（不配则每次重启所有会话失效） |
| `LUPIN_COOKIE_SECURE` | 生产 HTTPS 设为 `1` |
| `LUPIN_DB_AUTO_CREATE` | 默认 `1` 开箱建表；生产如走迁移则设 `0` 并跑 `alembic upgrade head` |

## 部署后检查

1. 打开 `https://你的域名/api/health`：`status: ok`、`ai_ready: true`、`ai_key_fp` 有值。
2. 注册/登录 → 店播生成：双击生成按钮只产生一个任务（查预算用量为单行）。
3. 未登录调 `/api/v1/ai-tasks` 应返回 401。
4. 拉呱版改写一次：响应含 `prompt_version: "colloquial_v2"`。
5. 首页 view-source 确认 OG 标签存在；手机尺寸首屏封面正常。
6. 生产库是空库，本地演示账号不存在，需要重新注册。

## 已知事项

- AI 生成走 deepkey 中转，长输出（>4 分钟）可能被中转断连，是基础设施限制，重试或拆短即可。
- 建议 cron：`scripts/expire_facts.py` 每天一次（事实过期标记）。
