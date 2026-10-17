# ADR-002：JWT 而非服务端 Session

- 状态：已采纳
- 日期：2026-10-17
- 相关：041-060（认证组）

## 背景

需要用户账号体系：登录、权限、租户隔离。CloudBase 云托管为无状态容器，
实例可能随时扩缩容/重建。

## 决策

采用 **JWT（Access Token + Refresh Token）**，无服务端 session 存储：

- Access Token：15 分钟，`Authorization: Bearer` 携带。
- Refresh Token：7 天，HttpOnly Cookie，用于换发 Access Token。
- 密钥 `LUPIN_JWT_SECRET` 从环境变量读；未配置时生成临时密钥并告警。

实现：`backend/auth/security.py`（签发/验签）、`backend/auth/deps.py::get_current_user`。

## 备选方案

1. **服务端 Session（Redis）**：需额外部署 Redis；容器重建不丢 session 但增加运维面。
   当前阶段 Redis 尚未引入，为认证单独引入过重。否决（未来引入 Redis 后可重评估）。
2. **Cookie Session（Flask 式）**：与 JWT 本质相同，但生态与 FastAPI 集成不如 JWT 成熟。否决。

## 后果

- 正面：无状态，扩缩容零改造；前后端分离天然适配。
- 负面：
  - Token 撤销需维护黑名单（当前未实现；登出仅靠客户端丢弃 + 短有效期缓解）。
  - 密钥轮换会导致全量 token 失效（需发公告）。
- 缓解：Access Token 15 分钟短有效期；重要操作（删作品、导出）要求二次验证（050）。

## 待办

- Refresh Token 黑名单/白名单（P1，配合 Redis 落地）。
- 管理员强制下线指定会话（053）。
