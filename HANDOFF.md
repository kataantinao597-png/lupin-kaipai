# 鲁品开拍 · 项目交接文档

> 交接时间：2026-10-16
> 目标：2026 山东省首届大学生 AI 大模型创新传播大赛，赛道三（AI 传播工具创新）
> 校内材料截止：2026-11-20 17:00

---

## 一、项目一句话

帮山东农产品/食品商家把产品信息和拍摄条件转化为可执行短视频拍摄方案的智能工具原型。

**公网地址**：https://lupin-kaipai-329881-11-1472147695.sh.run.tcloudbase.com
**GitHub**：https://github.com/kataantinao597-png/lupin-kaipai

---

## 二、技术栈

- 后端：FastAPI + Python 3.12（`backend/`）
- 前端：原生 HTML/JS/CSS，无框架（`frontend/`，JS 已按业务拆分为 10 个模块）
- 部署：Dockerfile → 腾讯云 CloudBase 云托管（CloudRun）
- AI：OpenAI 兼容接口，默认 deepkey.top 中转，模型 gpt-6-luna

---

## 三、已完成的功能（V1–V5）

- **V1**：产品录入 → 3 个创意方向 → 结构化分镜 → 镜头编辑/删除/排序/锁定 → 局部重生成 → 时长校验 → 风险提示 → DOCX 导出
- **V2**：拍摄可行性检查（规则预检 + 模型分析），替代方案一键采用
- **V3**：现场拍摄模式（待拍/已拍/补拍、进度统计、拍摄清单 DOCX）
- **V4**：多作品管理、工作台、localStorage 自动保存、阶段进度面板
- **V5**：产品事实库（已确认/待核实），生成内容引用事实，事实更新后过期提示
- **UI**：2026-10-16 重设计为「田野工作台」视觉（深田野绿 + 纸白 + 麦金）

---

## 四、当前状态 ⚠️ 待处理事项

### P0（必须做）

1. **AI 调用未通**：公网 `/api/ideas` 返回 `JSONDecodeError`
   - 根因：`LUPIN_API_BASE` 环境变量配成了 `https://deepkey.top/`（缺 `/v1`），请求打到网站首页 HTML 而非 API
   - 已做：代码层加了兜底（`ai_service.py` 自动把 `deepkey.top/chat/completions` 修正为 `deepkey.top/v1/chat/completions`），已提交到 git（commit `7532d4e`），**但尚未成功部署到公网**
   - 部署卡点：本机 `tcb` CLI 登录态不稳定 + 到腾讯云 API 的网络抖动（socket hang up），多次部署提交失败
   - **最稳的路**：用户从 CloudBase 控制台用「本地代码上传」方式重新部署，或等网络恢复后用 CLI 部署
   - 部署包位置：`/tmp/lupin-deploy/`（每次重启会丢，需从 git 重建）；Dockerfile 中已 bake 正确的 ENV（含 Key，**仅用于部署包，不在 git 中**）

2. **报名表未完成**：`~/workspace/your_files/附件2_参赛团队报名表_预填版.docx`
   - 已填：项目名、赛道三、作品形式、学校、团队名"星霄"、负责人王泽昊（性别/身份证/电话）、邮箱、作品简介（197字，含链接）、队员高秋雨、指导教师牟莉莉（仅姓名）
   - 还缺：牟莉莉的职务职称和电话、高秋雨的具体专业

### P1（建议做）

3. **真实用户验证**：尚无真实农户/商家使用过，全是自测数据
4. **SSRF 防护**：`ai_service.py` 只校验了 http/https 前缀，未做内网地址/DNS 复查
5. **CORS**：生产环境仍是 `allow_origins=["*"]`，可收紧为同源

### P2（不做）

- 演示视频：用户明确不录，只交作品

---

## 五、关键文件说明

| 路径 | 说明 |
|---|---|
| `backend/app.py` | FastAPI 入口，所有 API 路由 |
| `backend/ai_service.py` | AI 调用封装、重试、错误映射；含 deepkey 地址兜底 |
| `backend/prompts.py` | 全部 prompt 模板 |
| `backend/risk.py` | 风险检查 + 事实依据检查 |
| `backend/exporter.py` | DOCX 导出 |
| `frontend/index.html` | 单页结构 |
| `frontend/style.css` | 田野工作台视觉系统 |
| `frontend/js/` | 10 个业务模块（core/product/ideas/storyboard/risk/feasibility/shoot/works/workbench/facts） |
| `Dockerfile` | git 中的版本不含 Key；部署时需注入 `LUPIN_API_KEY`、`LUPIN_API_BASE`、`LUPIN_MODEL` |
| `competition-pack/` | 参赛材料（产品介绍、演示脚本、风险说明等） |

---

## 六、已知的坑

1. **`.hidden` 类**：曾未定义导致模型配置面板一直显示，已修复
2. **作品卡片标题**：`workbench.js` 曾把作品名塞进 `.num` 圆徽导致溢出，已改为 `.work-head`
3. **Docker 构建**：本机内核不支持 bridge，无法本地构建，必须走 CloudBase 云端构建
4. **tcb CLI**：登录态在 VM 重启后丢失；`tcb login --flow device` 有时网络失败
5. **旧版 localStorage 键**（`lupin_shoot_v1_*`）保留未迁移，不要删

---

## 七、测试

- `tests/test_export.py`：12/12 通过（DOCX 导出）
- `tests/test_risk.py`：8/8 通过
- 真浏览器（Firefox + playwright）回归通过，无 JS 错误

---

## 八、敏感信息提醒

- deepkey API Key：用户提供，仅用于部署配置，**不要写入 git、不要记入长期记忆**
- 用户及队友身份证号：仅用于填报名表，**不要记入长期记忆**
- GitHub PAT：已提醒用户删除

---

## 九、给下一个 Agent 的建议

1. 首要任务是把 AI 调用调通（P0-1），这是评委能感知的核心
2. 部署成功后做一次完整的公网端到端测试：建作品 → 填事实 → 生成创意 → 生成分镜 → 可行性 → 风险 → 导出 DOCX
3. 不要改技术栈（FastAPI + 原生前端），不要引入新依赖
4. 改样式前先截图对比，保持「田野工作台」方向
5. 所有 AI 相关的改动都要真测，不要 mock
