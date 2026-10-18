"""安全纵深防御包（Phase 9）。

- headers：安全响应头中间件（CSP report-only）
- ssrf：出站 URL 校验（内网 IP / 重定向 / DNS 重绑定防护）
- rate_limit：按用户+路由分级限流中间件（内存实现）
- upload：上传文件魔数校验与大小分级
- api_keys：集成用 API Key（服务账号）签发与校验
- audit：关键操作审计日志
"""
