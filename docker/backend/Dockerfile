# ── Stage 1: 安装 Python 依赖 ──
FROM python:3.10-slim-bookworm AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: 运行时镜像 ──
FROM python:3.10-slim-bookworm AS runtime
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PUID=1000 \
    PGID=1000

RUN apt-get update && apt-get install -y --no-install-recommends \
    supervisor \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
COPY --from=builder /install/bin /usr/local/bin

RUN find /usr/local/lib/python3.10/site-packages \
    \( -type d -name "__pycache__" -o \
       -type d -name "tests" -o \
       -type d -name "test" -o \
       -name "*.pyc" -o \
       -name "*.pyo" \) \
    -exec rm -rf {} + 2>/dev/null; \
    # 保留 dist-info，部分依赖会在运行时通过 importlib.metadata 读取包元数据。
    find /usr/local/lib/python3.10/site-packages -name "*.so" -exec strip --strip-debug {} + 2>/dev/null; \
    true

COPY docker/backend/docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh \
    && groupadd --system app \
    && useradd --create-home --shell /bin/bash --gid app app

COPY docker/backend/supervisord.conf /etc/supervisor/supervisord.conf
COPY .bundled-provider-artifacts /app/bundled-plugins
COPY src /app/src
RUN chmod -R a+rX /app/src

ARG SAKURAMEDIA_BACKEND_VERSION=v0.0.1
ENV SAKURAMEDIA_BACKEND_VERSION=${SAKURAMEDIA_BACKEND_VERSION}

# 主服务只保留自身运行所需的数据目录，嵌入模型由独立服务管理。
VOLUME ["/data"]
EXPOSE 8000 9001
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["start"]
