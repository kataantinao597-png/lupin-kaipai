# 鲁品开拍 MVP 开发进度（PROGRESS.md）

> 项目目录：`~/workspace/lupin-kaipai/` ｜ 开始：2026-10-16
> 原则：每阶段结束必须测试，结果写入此文件；不跳过测试；诚实标注 通过/未通过/未验证。

## 阶段 0：前置三个问题补齐

### 0.1 重试路径真实演练
- 状态：✅ 完成（2026-10-16）
- 测试：tests/test_retry_drill.py
  - A1-A5（单元，无模型）：缺字段/总时长错/序号不连续/单人手机航拍均被报出具体错误；正常数据通过
  - B1（真实模型+恒失败校验器）：恰好尝试 4 次（1+3重试），错误信息传回模型，最终抛用户可读错误"已重试 3 次…"
  - B2（真实模型+模拟首次缺字段）：第 2 次调用经错误反馈后成功，attempts=2
- 结果：9/9 通过。**实验阶段"重试路径未验证"的缺口已补上。**

### 0.2 风险检查三层
- 状态：✅ 完成（2026-10-16）
- 测试：tests/test_risk.py
  - t1/t1b：极限词/认证/农残关键词命中（"全国销量第一""降血压""有机""零农残"等）
  - t2：委婉暗示"每天吃一只，身体更轻松"规则层未命中（预期内），模型复核检出为功效暗示（分类B/D）——**实验阶段的 t2 部分漏报已修复**
  - t3：正常介绍无高风险误报
  - t4：德州扒鸡实战，输入卖点中的风险数据被标出（7条），且未进入旁白/字幕；flag 带 confirmed=False 人工确认入口
- 结果：8/8 通过。**不声称广告合规保证。**

### 0.3 DOCX 导出优化
- 状态：✅ 完成（2026-10-16）
- 测试：tests/test_export.py（案例A分镜生成后回读）
- 结果：12/12 通过。表头 tblHeader 跨页重复✅、所有行 cantSplit✅、35段落/1表格/11行、中文/风险/待确认完整。

## 阶段 1：FastAPI 骨架 + AI 服务层
- 状态：✅ 完成（2026-10-16）
- 内容：backend/app.py（/api/health + 静态前端托管 + 启动时 Key 进内存）；backend/ai_service.py（requests 调用、401/429/5xx 错误映射、退避重试、token 计数）；backend/prompts.py（创意/分镜/单镜重生成提示词，复用实验逻辑）；backend/schemas.py（Pydantic 模型）；backend/validator.py（业务规则）；backend/risk.py；backend/exporter.py
- 测试：uvicorn 真实启动，curl 验证 /api/health=200（ai_ready=true）、/=200 HTML、未知路由 404
- 结果：通过。venv 独立环境（fastapi 0.143.2 / pydantic 2.14.1），未污染系统包。

## 阶段 2：/api/ideas + /api/storyboard
- 状态：✅ 完成（2026-10-16）
- 测试：uvicorn 真实启动，案例A输入
  - POST /api/ideas → 200，3 个差异化方向（妈妈的苹果安全感/红富士脆甜实测/从果园到早餐桌），attempts=1
  - POST /api/storyboard → 200，8 镜头/60s 精确命中目标，attempts=1，risk_flags=1、pending=5、素材=7
  - 缺 product_name → 422；响应体无 Key 泄漏；health 显示 calls=2、tokens 累计正常
- 结果：通过。

## 阶段 3：/api/regenerate-shot + /api/risk-check + /api/export
- 状态：✅ 完成（2026-10-16）
- 测试（uvicorn 真实启动）：
  - POST /api/regenerate-shot → 200，按指令改了 shot2（俯视角度），锁定 shot1 与其他镜头逐字节未变，warning 为空；重生成锁定镜头 → 400"已锁定，不能重生成"
  - POST /api/risk-check → 200；发现模型复核过度标记（"红富士"标中风险）后优化 prompt（用户输入视为用户主张+只关注产品事实宣传）；优化后干净文案 0 标记、注入风险文案 7 条高亮；tests/test_risk.py 8/8
  - POST /api/export → 200，下载 docx 可打开（9行表格）；非法分镜 → 400 并给出具体校验错误
- 结果：通过。

## 阶段 4：前端四视图
- 状态：✅ 完成（2026-10-16）
- 内容：frontend/index.html（四视图骨架）+ style.css + app.js（原生 JS：表单→创意三选一→分镜编辑器→风险导出）
  - 编辑器：编辑/删除/上移/下移/锁定/解锁/单镜重生成；删除与移动后自动重排号并按对象身份重建锁定集合（已修 bug）；每次改动调 /api/validate 刷新校验条
  - 风险视图：三层检查结果卡片 + 人工确认勾选（同步回 storyboard.risk_flags）+ 素材清单 + 待确认事项 → 导出下载
  - 补加 POST /api/validate（编辑器校验用）
- 测试：node --check 通过；静态文件 200；案例B全流程 API 模拟（ideas→storyboard 8镜/45s精确→validate有效→regen shot3按指令改→risk 5条→export 40KB docx）；HTML/JS ID 一致性检查通过
- 结果：通过。已知局限：模型复核对普通文案偏谨慎（中/低标记较多），无高风险误报，人工确认层兜底；前端真实点击交互需浏览器人工验证（未验证）。

## 阶段 5：三案例回归
- 状态：✅ 完成（2026-10-16）
- 测试：tests/test_regression.py（uvicorn 真实启动，三案例全流程）
  - A烟台苹果：ideas 3个→storyboard→validate→risk 0条（文案干净）→export 39KB docx
  - B章丘大葱：45.0s/45.0s精确，单人手机零违规，序号连续，risk 5条，export 40KB
  - C德州扒鸡：60.0s/60.0s精确，风险数据（全国销量第一/降血压）被标出，export 40KB
- 结果：24/24 通过。回归记录：demo/regression.json

## 阶段 6：渲染检查 + 演示
- 状态：✅ 完成（2026-10-16）
- 测试：
  - 导出 docx → LibreOffice 转 PDF → 5 页 PNG 逐页目检：第1页标题/产品信息/分镜表头正常；第2-4页分镜表行完整无断裂；第5页风险提示（含待确认/已确认状态、来源）与待确认事项完整
  - 已知问题：表头跨页重复（tblHeader）XML 标记已正确写入（6 个表头单元格亲验），但 LibreOffice 渲染不重复（最小复现确认属渲染器限制）；Microsoft Word/WPS 会生效
  - Linux 下中文字体回退为 serif（微软雅黑未安装），Windows 端正常
  - 演示物：demo/拍摄执行单_演示.docx、.pdf、page-1~5.png、regression.json
- 结果：通过（1 项渲染器限制已记录）。前端真实浏览器点击截图：未验证（需人工在浏览器中操作）。

## 验收总表
| 验收项 | 结果 |
|---|---|
| 完整流程可走通 | ✅ 通过（三案例 API 全流程 24/24；前端代码完成，真实点击待浏览器验证） |
| 分镜严格校验 | ✅ 通过（Pydantic + 业务规则；破坏性输入 5/5 报出；非法分镜导出 400） |
| 单人约束生效 | ✅ 通过（B案例零违规；故意航拍输入被拦截） |
| 单镜可改可重生成且锁定保护 | ✅ 通过（API：锁定镜头重生成 400；其他镜头逐字节未变；前端锁定/解锁/重排逻辑已修 bug） |
| 风险内容提示+人工确认 | ✅ 通过（三层：规则+模型复核+confirmed 勾选；C案例风险数据被标出） |
| 重试路径真实测试过 | ✅ 通过（B1走满4次抛用户可读错误；B2缺字段第2次修复成功） |
| DOCX 可打开布局可读 | ✅ 通过（5页渲染目检；tblHeader 渲染器限制已记录） |
| 三案例复现记录 | ✅ 通过（demo/regression.json，24/24） |
| Key 不进前端/日志/代码 | ✅ 通过（启动时内存加载；响应体 grep 无泄漏；日志无 Key） |
