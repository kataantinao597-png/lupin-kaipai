# 鲁品开拍 - CloudBase 云托管 Dockerfile
# O-11（2026-11-10）：与 Dockerfile.prod 收敛——Python 3.12、非 root 用户
# appuser、健康检查参数与启动命令形式均已对齐；与 prod 唯一有意的
# 差异：① 单阶段构建（CloudBase 每次从源码构建，构建工具链无需剥离）；
# ② 启动不跑迁移（见下方 CMD 注释），迁移唯一落点是 Dockerfile.prod。
FROM python:3.12-slim

WORKDIR /app

# 先装依赖（利用 Docker 层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY backend/ ./backend/
COPY frontend/ ./frontend/
# 迁移脚本与配置：health 接口展示 migrations 版本，生产可用 alembic upgrade head
COPY alembic.ini ./
COPY migrations/ ./migrations/

# 非 root 用户（与 Dockerfile.prod 对齐，uid 10001）
RUN useradd -m -u 10001 appuser \
    && mkdir -p /app/backend/data /app/backend/backups \
    && chown -R appuser:appuser /app

USER appuser

# CloudBase 通过 PORT 环境变量指定端口，默认为 8080
ENV PORT=8080 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

# 健康检查（与 Dockerfile.prod 对齐：30s/5s/15s/3 次）
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/api/health', timeout=4)" || exit 1

# 启动只跑 uvicorn，不跑迁移。
# 迁移唯一落点是 Dockerfile.prod 的启动命令（alembic upgrade head）。
# 本镜像刻意不跑，原因（2026-11-10 已验证）：CloudBase 路径的库是容器内
# 临时 SQLite，由应用启动时 create_all 建表、不写 alembic_version 戳；
# 在这种库上跑 `alembic upgrade head` 会从 0001 重跑并以
# "table task_model_policy already exists" 失败（exit 1），容器将起不来。
# 而 create_all 本身已按当前模型建出 0023 的 schema（含 idempotency_keys
# 的 user_id 列与两条部分唯一索引），本路径不丢任何 schema。
# S1 单 worker 硬要求：配额提交锁与 poster 在途计数均为进程内状态，
# 多 worker 下锁与计数全部失效、额度竞态回归。扩容前必须先把在途计数
# 与占位行收进 DB 约束，届时再改 workers；此前禁止改回 2。
CMD ["sh", "-c", "exec uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --proxy-headers"]
