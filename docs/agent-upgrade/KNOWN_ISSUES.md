# 已知问题

## 2026-10-21：迁移误跑业务库（已恢复）
- 现象：子代理用 `set_main_option("sqlalchemy.url", …)` 定向 scratch 库做迁移验证，被 `migrations/env.py:29` 的 DATABASE_URL 覆盖，`alembic upgrade head` 误跑到 `backend/data/lupin.db`（0019→0020）。
- 恢复：立即 `downgrade 0019_ai_call_org`；独立验证 integrity ok、版本戳 0019、三表无残留、业务数据完好（48/2197/16）。
- 教训：**迁移类操作一律走 DATABASE_URL 环境变量定向**，绝不用 `set_main_option`。

## 长期已知（基线遗留）
- 6 个陈旧 pytest 用例失败（test_delivery_check×2、test_feasibility_fallback、test_handover_regressions×2、test_srt_route），基线即如此，与本次改动无关。
- `backend.models` 循环导入坑：不能作为进程内第一个 backend.* 导入，须先 import backend.auth.*。
- ~~`core/idempotency.py` 内存版幂等：多 worker/重启失效，Phase 7 补 DB 表。~~（2026-10-21 Phase 7 已修复：迁移 0021 `idempotency_keys` + `core/idempotency.py` 改 DB 实现，接口兼容）

## 2026-10-21：用户级每日限额延后（Phase 7 未对接）
- 现状：`quota_service` 只有组织维度；用户级每日限额（普通用户每天 5 次，2026-10-21 用户拍板）由另一 agent 实现，本分支未见真实实现。
- Phase 7 动作：保持 `backend/services/agent/context.py::check_user_quota()` 桩位不动（返回 `enforced=False`），**未自行实现限额功能**（那是另一个任务，见 PHASE1_DESIGN.md §1.3(4)/§7）。
- 待办：另一 agent 的限额实现落地后，在桩位处联调对接（返回口径 `{enforced, allowed, remaining, ...}` 保持不变）。

## 2026-10-21：Phase 8 评测与回归

- 新增测试 57 个（边界 49 + E2E/故障演练 8），评测脚本 25 场景 25/25 通过。
- 评测中发现并修复 2 个真实缺陷：
  1. 快照淘汰方向反了（`modify.py::_snapshot_work_version` 与 `works.py::_snapshot_work`）：超 50 个快照时删的是最新而非最旧，已改为按 `version_no` 降序淘汰最旧；
  2. 高风险关键词漏检"降血压/降血糖"（子串"降压"无法命中），`HIGH_RISK_KEYWORDS` 增补"血压/血糖"。
- 评测为 mock 驱动的确定性评测，不是生产流量：真实模型措辞/幻觉、真实用户输入多样性、并发压力、生产数据量级均未覆盖；上线前须跑 Phase 9 真实模型烟测。
- `_limited_repair`（步骤 8）在文案生成（步骤 9，可选）之前：文案中的高风险宣称走"门禁失败→partial"的安全失败路径而非自动修复，属 fail-safe 设计，非缺陷。
- 前端：4b 的 42 项 Playwright 检查已覆盖 agent 工作台；modify 流程尚未暴露 HTTP 路由（service 层），Phase 8 未新增前端用例。
