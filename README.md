# 鲁品开拍 · Web MVP

山东农产品短视频拍摄方案工具（2026 山东省首届大学生 AI 大模型创新传播大赛 · 赛道三）。

## 运行

```bash
cd ~/workspace/lupin-kaipai
./venv/bin/uvicorn backend.app:app --host 0.0.0.0 --port 8931
# 浏览器打开 http://127.0.0.1:8931/
# 说明：--host 0.0.0.0 允许局域网内其他设备访问；仅本机演示可用 --host 127.0.0.1
```

API Key 在启动时从 `~/.omp/agent/models.yml` 读入内存，不进前端/日志/代码。

## 流程

1. 填写产品资料与拍摄条件 → 生成 3 个创意方向
2. 选择方向 → 生成结构化分镜（Pydantic 校验 + 最多 3 次重试）
3. 分镜编辑器：编辑/删除/上移/下移/锁定/单镜重生成（锁定保护）
4. 风险检查（三层：规则初检 + 模型复核 + 人工确认）→ 导出 DOCX 执行单

## 测试

```bash
./venv/bin/python tests/test_retry_drill.py  # 重试路径（9项）
./venv/bin/python tests/test_risk.py          # 风险三层（8项）
./venv/bin/python tests/test_export.py        # DOCX 导出（12项）
./venv/bin/python tests/test_regression.py    # 三案例回归（需先启动服务，24项）
```

## 接口

- `POST /api/ideas` / `POST /api/storyboard` / `POST /api/regenerate-shot`
- `POST /api/risk-check` / `POST /api/validate` / `POST /api/export`
- `GET /api/health`

详见 `PROGRESS.md`（各阶段测试记录与验收表）。
