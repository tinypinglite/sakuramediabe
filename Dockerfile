# ── Stage 1: 安装 Python 依赖 ──
FROM python:3.10-slim-bookworm AS builder
WORKDIR /build
COPY requirements.txt .
COPY lib/metadata-provider ./lib/metadata-provider
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: 生成 PyArmor 混淆后的应用代码 ──
FROM python:3.10-slim-bookworm AS pyarmor-builder
WORKDIR /build
RUN pip install --no-cache-dir pyarmor==9.2.4
COPY src ./src
# PyArmor runtime 会生成在 /obfuscated 根目录，最终镜像需要和 src 一起复制。
RUN pyarmor gen -O /obfuscated -r src \
    && mkdir -p \
        /obfuscated/src/api \
        /obfuscated/src/service/catalog/prompts \
    && cp src/api/uvicorn_config.json /obfuscated/src/api/uvicorn_config.json \
    && cp src/service/catalog/prompts/*.md /obfuscated/src/service/catalog/prompts/

# ── Stage 3: 运行时基础镜像 ──
FROM python:3.10-slim-bookworm AS runtime-base
WORKDIR /app

ARG SAKURAMEDIA_BACKEND_VERSION=dev-local
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PUID=1000 \
    PGID=1000 \
    SAKURAMEDIA_BACKEND_VERSION=${SAKURAMEDIA_BACKEND_VERSION}

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

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh \
    && groupadd --system app \
    && useradd --create-home --shell /bin/bash --gid app app

COPY supervisord.conf /etc/supervisor/supervisord.conf
# 主服务只保留自身运行所需的数据目录，JoyTag 模型目录由独立推理服务管理。
VOLUME ["/data/db", "/data/cache/assets", "/data/cache/gfriends", "/data/indexes", "/data/logs", "/data/config"]
EXPOSE 8000 9001
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["start"]

# ── Stage 4: 发布用混淆镜像 ──
FROM runtime-base AS obfuscated
COPY --from=pyarmor-builder /obfuscated /app

# ── Stage 5: 默认普通运行时镜像 ──
FROM runtime-base AS runtime
COPY src /app/src
