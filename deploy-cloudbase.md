# CloudBase 云托管部署说明

## 部署方式（二选一）

### 方式 A：源码部署（推荐）

1. 将本目录代码推送到 Git 仓库（GitHub/Gitee/腾讯云 CODING）
2. 在 CloudBase 控制台 → 云托管 → 新建服务
3. 选择"代码部署"，关联仓库，构建方式选择 "Dockerfile"
4. 端口填写 `8080`
5. 配置环境变量（见下）
6. 点击部署，CloudBase 自动构建镜像并分配公网域名

### 方式 B：本地构建镜像后推送

```bash
cd ~/workspace/lupin-kaipai
docker build -t lupin-kaipai:latest .
# 推送到腾讯云容器镜像服务后，在云托管中选择该镜像
```

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `PORT` | 否 | 平台自动注入，默认 8080 |
| `LUPIN_API_KEY` | 二选一 | 服务默认 API Key；如不设，用户需在前端自行配置 |
| `LUPIN_API_BASE` | 否 | 默认 `https://deepkey.top/v1/chat/completions` |
| `LUPIN_MODEL` | 否 | 默认 `gpt-6-luna` |

**注意**：如不配置 `LUPIN_API_KEY`，评委需在前端"模型配置"中填写自己的 Key（仅存于浏览器会话，不上传服务器存储）。

## 本地验证（不等价于容器构建）

```bash
PORT=8080 ./venv/bin/uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-8080}
curl http://127.0.0.1:8080/api/health
```
