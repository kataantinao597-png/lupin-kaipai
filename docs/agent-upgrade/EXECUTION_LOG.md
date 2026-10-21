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

## Phase 2 — 工具层实现（子代理执行中）

- 范围：`backend/services/agent/` 四模块 + alembic 迁移（三表）+ `tests/test_agent_tools_phase2.py`
- 红线已传达：scratch DB、429 硬停、视频 key 禁用、密钥不进 git
