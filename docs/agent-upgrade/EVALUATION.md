# Agent 升级 Phase 8：固定评测集

> 任务书 §十二。运行器：`scripts/eval_phase8.py`（25 个脚本化场景），
> 结果 JSON：`docs/agent-upgrade/eval_phase8_results.json`。
> 运行：`./venv/bin/python scripts/eval_phase8.py`（约 10 秒）。

## ⚠️ 诚实标注

- 这是 **mock 模型驱动的确定性评测**，所有"模型"调用均为按 prompt 标记
  返回固定 JSON 的 fake 实现（见 `scripts/eval_phase8.py::_brain_oneshot`）。
- **不是生产流量**：未覆盖真实模型的措辞多样性、真实用户的奇葩输入、
  生产数据分布、并发压力与长时间运行。
- "通过"只代表"在该脚本化输入下，代码按设计行为执行"；真实模型上线前
  仍需 Phase 9 的真实模型烟测（任务书 §十三）。

## 评测结果（2026-10-21，25/25 通过）

| ID | 类别 | 输入 | 预期 artifact / 行为 | 实际结果 | 结论 |
|---|---|---|---|---|---|
| E01 | 正常 | 一句话："给烟台苹果做条抖音带货视频" | completed + 5 artifacts 落盘 | 5 artifacts 齐，work 已持久化 | ✅ |
| E02 | 正常 | 两轮 brief（第二轮复用 conversation_id） | 同一会话，brief 增量合并 | brief.product_name+platform 合并 | ✅ |
| E03 | 正常 | "改文案标题"（局部修改） | 仅 copy 变化 | 仅 copy 变化，其余零变化 | ✅ |
| E04 | 正常 | "预算改 300"（字段替换） | brief.budget_yuan=300 | =300，无模型重写 | ✅ |
| E05 | 正常 | 修改后一键恢复 | 数据与修改前一致 | 完全一致 | ✅ |
| E06 | 正常 | plan：query_facts + query_products | 真实 adapter 读到种子行 | 事实/产品均命中 | ✅ |
| E07 | 正常 | 导出 export-json | 含作品数据 + 版本历史 | versions 含快照 | ✅ |
| E08 | 边界 | 只有产品名的极简 brief | 透明默认值 + confirmed_defaults | 3 项默认被标记 | ✅ |
| E09 | 边界 | 空字符串 brief | failed + 可读错误 | "brief 解析失败…" | ✅ |
| E10 | 边界 | brief 中 budget_yuan=0 | 视为"未指定"→None | None（设计如此） | ✅ |
| E11 | 边界 | 4000 字超长 brief | 截断 2000 存 brief_json | 存 2000 字 | ✅ |
| E12 | 边界 | 改产品名（人工确认事实） | waiting_user + confirmed_fact | 原因正确，未执行写操作 | ✅ |
| E13 | 边界 | 分镜口播含"降血压"（无事实） | 自动标"待核实"并 completed | repaired=1，warnings 有记录 | ✅ |
| E14 | 边界 | 可选步骤工具失败 | partial（必需步骤成功） | partial | ✅ |
| E15 | 边界 | plan 含白名单外 tool（exec_shell） | set_run_plan 拒绝 | ValueError，白名单错误 | ✅ |
| E16 | 边界 | plan 26 步（超 25 上限） | 拒绝 | 步数超限错误 | ✅ |
| E17 | 失败 | 工具抛 TimeoutError | 快速失败、无重试 | 1 次调用，<10s 失败 | ✅ |
| E18 | 失败 | 策略步骤撞 429 | 硬停，后续零模型调用 | 无分镜/文案调用 | ✅ |
| E19 | 失败 | brief 解析撞 500 | 直接失败，不重试 | 1 次调用 | ✅ |
| E20 | 对抗 | 调 exec_shell / params 塞 org_id | 白名单拒绝 | unknown_tool | ✅ |
| E21 | 对抗 | 跨组织读 run | found=False（404 防枚举） | 本租户可读，跨租户不可见 | ✅ |
| E22 | 对抗 | 会话 A 的作品改作品 B | EntityNotFound 拒绝 | 零写操作 | ✅ |
| E23 | 对抗 | 同幂等键重复提交建 run | 同一 task_id，不重复建任务 | 1 个任务 + replay 标记 | ✅ |
| E24 | 对抗 | stale expected_version 调 patch_work | conflict，不覆盖 | 数据保持 v2 | ✅ |
| E25 | 对抗 | 执行中取消 | cancelled，已成功步骤保留 | 停步，状态 cancelled | ✅ |

## 质量指标（实测值，2026-10-21）

| 指标 | 目标 | 实测 | 证据 |
|---|---|---|---|
| 核心闭环成功率（一句话→completed） | ≥90% | **100%**（3/3：phase5 happy path、E2E test_full_loop、评测 E01） | `test_agent_orchestrator_phase5.py::TestHappyPath`、`test_agent_e2e_phase8.py::TestAgentE2E` |
| 必需工具选择/执行正确率 | ≥95% | **100%**：白名单校验 13/13（TestToolSchemas 6 + TestPlanner 7）；真实 adapter 执行 E06/E2E 步骤 5 全部 ok | `test_agent_tools_phase2.py`（38 全绿）、E2E |
| 跨租户/跨作品越权 | 0 次 | **0** | phase7 TestCrossTenant 9/9、评测 E21/E22、边界 test_verify_persistence_wrong_org |
| 未确认高风险事实被作为确定性宣传依据 | 0 次 | **0**（mock 覆盖内） | E13（自动标待核实）、phase5 TestRiskyContent、边界 test_fact_grounding_*（4） |
| 局部修改时无关内容未经授权变化 | 0 次 | **0** | E03、E2E 步骤 6、phase6 TestTypicalRequests（逐字段断言零变化） |
| 重复提交造成重复作品/重复扣费 | 0 次 | **0** | E23、phase7 TestIdempotencyDB 4/4（含并发双击） |
| UI 展示的状态与后端终态不一致 | 0 次 | **0**（Phase 8 未新增前端用例；沿用 4b 的 42 项 Playwright 检查结论） | 4b 报告（见 EXECUTION_LOG）；本 Phase 未发现新的不一致 |

### 评测中发现并修复的问题（2）

1. **快照淘汰方向反了**（`_snapshot_work_version` / `works.py::_snapshot_work`）：
   超 `MAX_VERSIONS_PER_WORK=50` 时代码删的是**最新**快照，注释写的是"删最旧"。
   已修复为按 `version_no` 降序取、淘汰最旧；边界测试 `test_snapshot_version_cap`
   锁定该行为。属 Phase 8 测试发现的真实缺陷（此前无测试覆盖该分支）。
2. **高风险关键词漏检"降血压/降血糖"**：子串"降压/降糖"无法命中"降血压/降血糖"
   （"降"+"压"不相邻）。`HIGH_RISK_KEYWORDS` 增补"血压/血糖"；
   回归测试 `test_fact_grounding_blood_sugar_keyword` + 评测 E13 锁定。

### 已知局限（未覆盖，见 KNOWN_ISSUES）

- 真实模型行为差异：mock 保证确定性，但真实模型的措辞/幻觉/长短不受控，
  上线前必须跑 Phase 9 真实模型烟测。
- 并发压力、长时间运行、生产数据量级未评测。
- 前端 waiting_user 整流等交互细节：4b 已做 42 项检查，Phase 8 未补新用例
  （modify 流程尚未暴露 HTTP 路由，service 层已全覆盖）。
- 评测 E13 注记：`_limited_repair`（步骤 8）在文案生成（步骤 9，可选）之前，
  文案中的高风险宣称走"门禁失败→partial"的安全失败路径，而非自动修复；
  这是符合 fail-safe 的设计，未视为缺陷。
