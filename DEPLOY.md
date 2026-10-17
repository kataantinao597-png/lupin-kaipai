# 鲁品开拍 · 部署方法

## 安全原则（必读）

- **密钥绝不进镜像、代码和 git**：`LUPIN_API_KEY` 等密钥一律通过**运行时环境变量**注入。
  Dockerfile 中只保留 `PORT` 等非敏感默认值，不写任何密钥。
- 密钥只出现在两个地方：你本地的环境变量（或 `~/.omp/agent/models.yml`，600 权限）、CloudBase 控制台的环境变量配置页。
- ⚠️ **历史教训**：曾经有把密钥 bake 进 Dockerfile 的部署方式，现已废弃。
  任何已构建的含密钥镜像都应视为**密钥已泄露**——请立即去 deepkey 控制台轮换 Key，并删除相关镜像与构建记录。

## 需要配置的环境变量

| 变量 | 说明 | 值 |
|---|---|---|
| `LUPIN_API_BASE` | 模型 API 地址 | `https://deepkey.top/v1` |
| `LUPIN_API_KEY` | 模型 API Key | （从 deepkey 控制台获取，不写入任何文件） |
| `LUPIN_MODEL` | 模型名 | `gpt-6-luna` |

三种部署方式都要配置这三个变量（位置：控制台云托管服务的环境变量配置页）。

代码层有 deepkey 地址兜底（`ai_service.py`）：即使配成 `https://deepkey.top/` 也会自动修正为 `/v1` 路径。

## 方式一：CloudBase 控制台上传代码（推荐，最稳）

1. 打开 https://tcb.cloud.tencent.com，进入环境 `lupin-kaipai-d6gwx3froc49a3bd2`
2. 左侧 **云托管** → 新建服务（旧的 `lupin-kaipai` 如需替换先删除）
3. 代码来源选 **本地代码**（代码包上传），上传本仓库源码 zip（Dockerfile 为仓库版本，不含密钥）
4. 构建方式：**Dockerfile**；Dockerfile 路径：`Dockerfile`
5. 服务端口：`8080`；公网访问：开启
6. 在 **环境变量** 配置中填写上表三个变量（`LUPIN_API_KEY` 填真实 Key）
7. 点部署，等 3–8 分钟

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

部署完成后，去控制台云托管服务详情页配置环境变量（同上三个变量），服务会自动重启生效。
（CloudBase 官方文档即要求 CLI 部署后在服务详情页配置环境变量。）

注意：CLI 登录态不稳定，VM 重启后会丢；到腾讯云 API 的网络有时抖动。

## 方式三：GitHub 自动部署

1. 仓库：https://github.com/kataantinao597-png/lupin-kaipai
2. CloudBase 控制台 → 云托管 → 新建服务 → 代码来源选 GitHub → 授权 → 选仓库 → 分支 main
3. 构建方式 Dockerfile，服务端口 8080
4. 在 **环境变量** 配置中填写上表三个变量（`LUPIN_API_KEY` 填真实 Key）
5. 开启自动部署，push 即自动重新部署

注意：
- GitHub OAuth 授权有时效问题，构建时可能报 `could not read Username`，重授权选 All repositories 可解。
- 密钥只填在控制台环境变量页，**不要提交到仓库的任何文件**（包括 Dockerfile、.env）。

## 验证部署成功

```bash
# 健康检查
curl https://<你的域名>/api/health
# 应返回 {"status":"ok","ai_ready":true,...}

# AI 调用测试（验证密钥真的生效，不能只看健康接口）
curl -X POST https://<你的域名>/api/ideas \
  -H "Content-Type: application/json" \
  -d '{"product":{"product_name":"测试"},"facts":[]}'
# 应返回 3 个创意 JSON，而不是 500
```

## 密钥泄露应急

如果 Key 曾出现在聊天记录、Docker 镜像层、构建日志或 git 历史中：

1. 立即去 deepkey 控制台删除/轮换该 Key
2. 删除含密钥的镜像与构建产物
3. 用新 Key 更新控制台环境变量并重新部署
