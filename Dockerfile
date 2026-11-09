# 鲁品开拍 - CloudBase 云托管 Dockerfile
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

# CloudBase 通过 PORT 环境变量指定端口，默认为 8080
ENV PORT=8080
EXPOSE 8080

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:' + __import__('os').environ.get('PORT','8080') + '/api/health', timeout=4)" || exit 1

CMD ["sh", "-c", "uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --proxy-headers"]
