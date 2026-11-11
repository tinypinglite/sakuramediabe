# ── Stage 1: 安装 Python 依赖 ──
FROM python:3.10-slim-bookworm@sha256:68d914ec641a0b69267ce65184d000a2bc3a9ee2590ab702b82250ab2385735a AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: 内置官方插件包（仅 CI 发布构建启用）──
# SAKURAMEDIA_BUNDLED_PLUGINS 默认 0：本地手动 docker build 不下载任何插件，
# /app/bundled-plugins 只是空目录，容器启动时的内置安装步骤自动跳过。
# 版本不写死：构建期取各插件仓库的 latest 正式 Release；需要复现历史镜像时用
# SAKURAMEDIA_BUNDLED_PLUGIN_PINS="sakuramedia_local_provider=v0.2.6" 固定单个插件。
# 取插件的结果依赖 GitHub 上的外部状态，不在 build arg 里；BuildKit 缓存只认
# 命令与 build arg，于是同一份源码的重复发版会直接复用旧层、把上一版插件固化进镜像。
# 发布流水线传 SAKURAMEDIA_BUNDLED_PLUGINS_REFRESH=<本次 tag>，让每次发版都重新解析
# 插件 latest Release。
FROM python:3.10-slim-bookworm@sha256:68d914ec641a0b69267ce65184d000a2bc3a9ee2590ab702b82250ab2385735a AS bundled
ARG SAKURAMEDIA_BUNDLED_PLUGINS=0
ARG SAKURAMEDIA_BUNDLED_PLUGIN_PINS=""
ARG SAKURAMEDIA_BUNDLED_PLUGINS_REFRESH=""
COPY docker/backend/fetch_bundled_plugins.py /opt/bundled/fetch_bundled_plugins.py
# token 通过 BuildKit secret 传入，不进镜像层也不进 docker history；缺失时回退匿名请求。
RUN --mount=type=secret,id=github_token,required=false \
    set -eu; \
    echo "bundled plugins refresh=${SAKURAMEDIA_BUNDLED_PLUGINS_REFRESH}"; \
    if [ "${SAKURAMEDIA_BUNDLED_PLUGINS}" = "1" ]; then \
        token=""; \
        if [ -f /run/secrets/github_token ]; then token="$(cat /run/secrets/github_token)"; fi; \
        pins=""; \
        for pin in ${SAKURAMEDIA_BUNDLED_PLUGIN_PINS:-}; do pins="${pins} --pin ${pin}"; done; \
        python /opt/bundled/fetch_bundled_plugins.py --mode fetch --output /bundled --token "${token}" ${pins}; \
    else \
        echo "bundled plugins disabled (SAKURAMEDIA_BUNDLED_PLUGINS=0)"; \
        mkdir -p /bundled; \
    fi

# ── Stage 3: GFriends Filetree 构建期 seed ──
# 运行时仍会按 TTL 刷新 /data 下的缓存；这里只保证首启和离线环境有只读兜底。
# 发布流水线可传 SAKURAMEDIA_GFRIENDS_REFRESH=<本次 tag> 让 BuildKit 重新下载。
FROM python:3.10-slim-bookworm@sha256:68d914ec641a0b69267ce65184d000a2bc3a9ee2590ab702b82250ab2385735a AS gfriends
ARG SAKURAMEDIA_GFRIENDS_SEED=1
ARG SAKURAMEDIA_GFRIENDS_REFRESH=""
ARG GFRIENDS_FILETREE_URL="https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/Filetree.json"
COPY docker/backend/fetch_gfriends_filetree.py /opt/gfriends/fetch_gfriends_filetree.py
RUN set -eu; \
    echo "gfriends seed=${SAKURAMEDIA_GFRIENDS_SEED} refresh=${SAKURAMEDIA_GFRIENDS_REFRESH}"; \
    mkdir -p /out; \
    if [ "${SAKURAMEDIA_GFRIENDS_SEED}" = "1" ]; then \
        GFRIENDS_FILETREE_URL="${GFRIENDS_FILETREE_URL}" \
            python /opt/gfriends/fetch_gfriends_filetree.py; \
    else \
        echo "gfriends seed disabled"; \
    fi

# ── Stage 4: 运行时镜像 ──
FROM python:3.10-slim-bookworm@sha256:68d914ec641a0b69267ce65184d000a2bc3a9ee2590ab702b82250ab2385735a AS runtime
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
# GFriends 构建期 seed：位于 /app 下，不会被 /data 运行时卷遮蔽。
COPY --from=gfriends /out /app/metadata-data
# 内置插件包：本地构建为空目录（安装步骤跳过），CI 发布构建为已校验的 ZIP + 索引。
COPY --from=bundled /bundled /app/bundled-plugins
COPY src /app/src
RUN chmod -R a+rX /app/src /app/bundled-plugins

ARG SAKURAMEDIA_BACKEND_VERSION=v0.0.1
ENV SAKURAMEDIA_BACKEND_VERSION=${SAKURAMEDIA_BACKEND_VERSION}

# 主服务只保留自身运行所需的数据目录，嵌入模型由独立服务管理。
VOLUME ["/data"]
EXPOSE 8000 9001
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["start"]
