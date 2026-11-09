# 鲁品开拍部署包（Agent 商用级升级版）

- 分支：`feat/agent-workspace-commercial`，commit `590d288`（2026-10-22）
- 内容：Agent 对话式升级全量 + 海报生图限额/备选通道 + AI.md 验收修复
- 验收结论：PASS（详见 `docs/agent-upgrade/ACCEPTANCE_MATRIX.md`）

## 部署方式（workbuddy 路线）

用本包内容生成干净 staging → 发布到应用槽（覆盖旧版）。环境变量在平台侧单独配置，不进代码。

## 环境变量（平台侧配置）

| 变量 | 值 | 说明 |
|---|---|---|
| `LUPIN_JWT_SECRET` | （用户提供） | JWT 签名密钥，生产必须配 |
| `LUPIN_API_BASE` | `https://deepkey.top` | 文本生成 API base |
| `LUPIN_API_KEY` | （用户 deepkey luna key） | 文本生成 key |
| `LUPIN_MODEL` | `gpt-6-luna` | 文本生成模型 |
| `LUPIN_COOKIE_SECURE` | `1` | 生产 Cookie 安全标志 |
| `LUPIN_IMAGE_API_BASE` | `https://deepkey.top/v1` | 海报生图 base |
| `LUPIN_IMAGE_API_KEY` | （用户 deepkey luna key） | 海报生图主 key |
| `LUPIN_IMAGE_MODEL` | `gpt-6-luna` | 海报生图主模型 |
| `LUPIN_IMAGE_API_BASE_FALLBACK` | `https://deepkey.top/v1` | 海报生图备选 base（可选） |
| `LUPIN_IMAGE_API_KEY_FALLBACK` | （用户 deepkey gpt key） | 海报生图备选 key：主通道超时/429/5xx/连接失败时自动切换 |
| `LUPIN_IMAGE_MODEL_FALLBACK` | `gpt-image-2` | 海报生图备选模型 |

**key 获取**：luna key 在用户保险箱 `custom.deepkey-luna`，gpt key 在 `custom.deepkey-gpt`，部署时由用户本人提供，绝不写进代码包。

## 数据库

- 首次部署：alembic 迁移自动补跑（含 `0022_poster_image_usage` 海报生图用量表）
- 已有数据：迁移向前后兼容，不要删除 `backend/data/lupin.db`（如平台有持久化卷）

## 部署后冒烟（用测试账号）

1. 登录 `admin@lupin.test` / `Lupin@2026`，确认左侧导航 11 个入口（含「模型配置」）
2. AI 创作助手发一句话需求，确认任务从排队→执行→完成（不再卡 pending）
3. 海报制作 → AI 生图，普通用户确认剩余额度显示，用 1 次后额度 -1
4. 导出一份 DOCX，确认分镜时长/镜号正常

## 已知事项

- 6 个陈旧 pytest 用例基线失败（test_delivery_check×2、test_feasibility_fallback、test_handover_regressions×2、test_srt_route），与本次改动无关
- `allow_origins=["*"]` 未收紧（待域名白名单）
- Cookie CSRF 仅 SameSite=lax
- 定价模块为占位价格，用户未拍板
