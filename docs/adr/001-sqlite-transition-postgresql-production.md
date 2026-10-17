# ADR-001：SQLite 过渡 + PostgreSQL 生产

- 状态：已采纳
- 日期：2026-10-17
- 相关：081-100（数据库组）

## 背景

项目需要服务端持久化（作品、分镜、事实库不能只放浏览器 localStorage）。
团队无专职 DBA，开发机无常驻数据库服务，CI 需零依赖运行。

## 决策

- **开发/测试/过渡期**：SQLite（文件 `backend/data/lupin.db`），SQLAlchemy ORM + Alembic 管理。
- **生产**：PostgreSQL，通过 `DATABASE_URL` 切换，无需改业务代码。
- 判定逻辑在 `backend/auth/database.py::resolve_database_url()`：
  `DATABASE_URL` 为空 → SQLite；以 `postgres` 开头 → PostgreSQL。

## 备选方案

1. **直接上 PostgreSQL**：开发机需常驻服务，CI 复杂化，新人上手成本高。否决。
2. **继续 localStorage**：刷新/换设备丢数据，无法做租户隔离与审计。否决。
3. **MySQL**：JSON 字段与数组支持弱于 PG，且团队无 MySQL 运维经验。否决。

## 后果

- 正面：零依赖启动；Alembic 迁移在两种库上同一套脚本；Repository 层屏蔽差异。
- 负面：SQLite 与 PG 在并发写入、JSON 查询上有语义差异；上线前必须在 PG 上跑全量回归。
- 缓解：`tests/test_db_phase3.py` 用 SQLite 测逻辑；PG 只测 URL 解析与 engine 构建（无 PG 服务时跳过建连）。

## 待办

- 生产部署后做一次 PG 恢复演练（090）。
- 高并发场景评估是否需要读写分离（P2）。
