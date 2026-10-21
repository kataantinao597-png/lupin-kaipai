# 已知问题

## 2026-10-21：迁移误跑业务库（已恢复）
- 现象：子代理用 `set_main_option("sqlalchemy.url", …)` 定向 scratch 库做迁移验证，被 `migrations/env.py:29` 的 DATABASE_URL 覆盖，`alembic upgrade head` 误跑到 `backend/data/lupin.db`（0019→0020）。
- 恢复：立即 `downgrade 0019_ai_call_org`；独立验证 integrity ok、版本戳 0019、三表无残留、业务数据完好（48/2197/16）。
- 教训：**迁移类操作一律走 DATABASE_URL 环境变量定向**，绝不用 `set_main_option`。

## 长期已知（基线遗留）
- 6 个陈旧 pytest 用例失败（test_delivery_check×2、test_feasibility_fallback、test_handover_regressions×2、test_srt_route），基线即如此，与本次改动无关。
- `backend.models` 循环导入坑：不能作为进程内第一个 backend.* 导入，须先 import backend.auth.*。
- `core/idempotency.py` 内存版幂等：多 worker/重启失效，Phase 7 补 DB 表。
