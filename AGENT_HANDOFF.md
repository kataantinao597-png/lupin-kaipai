# 鲁品开拍 - Agent 交接包

## 项目定位

山东特色农产品 AI 营销与短视频创作工作台。前端是原生 HTML/CSS/JavaScript，后端是 FastAPI，支持产品档案、事实库、创意方向、分镜编辑、拍摄执行、风险审核和 DOCX/SRT 交付。

## 当前入口

- 本地工作台：http://127.0.0.1:8942/
- 后端入口：backend/app.py
- 前端入口：frontend/index.html
- 前端静态资源：frontend/style.css、frontend/heritage.css、frontend/js/

## 当前已完成的修复

- 工作台总览已调整为“田野档案馆”方向：作品档案行、阶段进度轨道、山东非遗轻量印章/纸边/地域标签。
- 工作台动效已加入：入场、纹理缓慢移动、印章落位、进度线流光，并支持 prefers-reduced-motion。
- 分镜编号徽标已修复：“镜头 1”不再因为固定宽度换行，见 frontend/heritage.css。
- AI JSON 解析已增强：兼容纯 JSON、Markdown 代码围栏、少量前后说明文字和结构化文本块，见 backend/ai_service.py 中的 parse_json_content。
- API Key 不写入前端持久存储；前端配置使用当前浏览器会话，服务端通过环境变量或请求头读取。

## 运行

PowerShell：
cd lupin-kaipai
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8942

需要 AI 时配置环境变量，不要把真实 Key 写入代码或文档：
$env:LUPIN_API_KEY = "<your-key>"
$env:LUPIN_API_BASE = "https://deepkey.top/v1"
$env:LUPIN_MODEL = "gpt-6-luna"

也可以在工作台的“模型配置”入口填写会话级配置。

## 验证

python -m pytest -q tests/test_handover_regressions.py tests/test_srt_route.py tests/test_srt_export.py tests/test_delivery_check.py tests/test_feasibility_fallback.py
node --check frontend/js/workbench.js
node --check frontend/js/storyboard.js

完整测试中的部分用例依赖外部 /home/hatch/... 实验目录或模型配置，换机器后应先检查这些路径和环境变量。

## 修改约束

- 保留现有业务逻辑、作品隔离、自动保存、镜头锁定、阶段进度、导出和 AI 请求行为。
- 修改前先检查真实源码和页面，不要推倒重写。
- UI 以现代工作台为主体，非遗元素只做轻量纹样、印章、纸感边饰和地域标签。
- 桌面端和手机端都要检查；所有动态效果尊重 prefers-reduced-motion。
- 不要在日志、截图、提交或交接文档中写入真实 API Key。

## 打包排除项

本交接包不包含 .pytest_cache、tmp、__pycache__、.pyc、本地日志和环境密钥文件；这些属于机器状态，不是项目源码。
