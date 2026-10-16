# 鲁品开拍 · 部署方法

## 方式一：CloudBase 控制台（推荐，最稳）

1. 打开 https://tcb.cloud.tencent.com，进入环境 `lupin-kaipai-d6gwx3froc49a3bd2`
2. 左侧 **云托管** → 删除旧的 `lupin-kaipai` 服务（如有）
3. 点 **新建服务** → 代码来源选 **本地代码**（代码包上传）
4. 上传 `lupin-kaipai-deploy.zip`（见本包，配置已内置）
5. 构建方式：**Dockerfile**；Dockerfile 路径：`Dockerfile`
6. 服务端口：`8080`；公网访问：开启
7. 环境变量：**不用填**（已 bake 在镜像里）
8. 点部署，等 3–8 分钟

## 方式二：tcb CLI

```bash
npm install -g @cloudbase/cli
tcb login --flow device   # 按提示授权
cd lupin-kaipai
tcb cloudrun deploy \
  --env-id lupin-kaipai-d6gwx3froc49a3bd2 \
  --service-name lupin-kaipai \
  --source . \
  --port 8080 \
  --open-access-types PUBLIC \
  --wait --force
```

注意：CLI 登录态不稳定，VM 重启后会丢；到腾讯云 API 的网络有时抖动。

## 方式三：GitHub 自动部署

1. 仓库：https://github.com/kataantinao597-png/lupin-kaipai
2. CloudBase 控制台 → 云托管 → 新建服务 → 代码来源选 GitHub → 授权 → 选仓库 → 分支 main
3. 构建方式 Dockerfile，服务端品 8080
4. 环境变量手动填：
   - `LUPIN_API_BASE` = `https://deepkey.top/v1`
   - `LUPIN_API_KEY` = （deepkey 的 key）
   - `LUPIN_MODEL` = `gpt-6-luna`
5. 开启自动部署，push 即自动重新部署

注意：GitHub OAuth 授权有时效问题，构建时可能报 `could not read Username`，重授权选 All repositories 可解。

## 环境变量说明

| 变量 | 说明 | 默认值（代码中） |
|---|---|---|
| `LUPIN_API_BASE` | 模型 API 地址 | `https://deepkey.top/v1/chat/completions` |
| `LUPIN_API_KEY` | 模型 API Key | 无，必须配置 |
| `LUPIN_MODEL` | 模型名 | `gpt-6-luna` |
| `PORT` | 服务端口 | `8080` |

代码层有 deepkey 地址兜底（`ai_service.py`）：即使配成 `https://deepkey.top/` 也会自动修正为 `/v1` 路径。

## 验证部署成功

```bash
# 健康检查
curl https://<你的域名>/api/health
# 应返回 {"status":"ok","ai_ready":true,...}

# AI 调用测试
curl -X POST https://<你的域名>/api/ideas \
  -H "Content-Type: application/json" \
  -d '{"product":{"product_name":"测试"},"facts":[]}'
# 应返回 3 个创意 JSON，而不是 500
```

## 本包文件说明

- `lupin-kaipai/`：完整项目源码（含 HANDOFF.md 交接文档）
- `lupin-kaipai-deploy.zip`：可直接上传部署的代码包（Dockerfile 已内置正确配置）
- `skills/`：本项目 UI 设计用到的 skill（见下）
