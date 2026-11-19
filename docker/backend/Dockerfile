# ── Stage 1: 安装 Python 依赖 ──
FROM python:3.10-slim-bookworm@sha256:68d914ec641a0b69267ce65184d000a2bc3a9ee2590ab702b82250ab2385735a AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt
# supervisor 走 pip 而不是 apt：避免 apt 额外带进一套 python3.11（约 22MB）。
RUN pip install --no-cache-dir --prefix=/install supervisor==4.2.5

# ── Stage 2: Nuitka 编译宿主 ──
# 只编译 src 包；第三方依赖全部 nofollow，运行时从 site-packages 正常 import。
FROM builder AS nuitka
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libc6-dev binutils \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir nuitka==4.3.5
WORKDIR /build
COPY src /build/src
COPY docker/backend/gen_migration_manifest.py /opt/gen_migration_manifest.py
RUN python /opt/gen_migration_manifest.py \
        /build/src/start/migrations/versions \
        /build/src/start/migrations/versions.json
ENV PYTHONPATH=/install/lib/python3.10/site-packages
RUN NOFOLLOW="pydantic pydantic_settings pydantic_core fastapi starlette uvicorn httpx httpcore anyio sniffio loguru typing_extensions av PIL pillow_heif numpy qdrant_client peewee apscheduler toml tomli packaging bcrypt jose cryptography tqdm xmltodict multipart python_multipart psycopg2 opencc psutil certifi bs4 soupsieve portalocker qbittorrentapi libtorrent click" \
    && FLAGS="" \
    && for p in $NOFOLLOW; do FLAGS="$FLAGS --nofollow-import-to=$p"; done \
    && python -m nuitka --mode=package src \
        $FLAGS \
        --output-dir=/build/nuitka-out \
        --remove-output \
        --assume-yes-for-downloads \
        --file-reference-choice=runtime \
        --jobs=2 \
        --low-memory

# ── Stage 3: 内置官方插件包（仅 CI 发布构建启用）──
# SAKURAMEDIA_BUNDLED_PLUGINS 默认 0：本地手动 docker build 不下载任何插件，
# /app/bundled-plugins 只是空目录，容器启动时的内置安装步骤自动跳过。
# 版本不写死：构建期从插件市场 index.json 读取各插件的 latest 正式版本；需要复现历史
# 镜像时用 SAKURAMEDIA_BUNDLED_PLUGIN_PINS="sakuramedia_local_provider=v0.2.6" 固定
# 单个插件（pin 走市场仓的 <plugin_id>-v<version> Release）。
# 取插件的结果依赖市场 index 的外部状态，不在 build arg 里；BuildKit 缓存只认
# 命令与 build arg，于是同一份源码的重复发版会直接复用旧层、把上一版插件固化进镜像。
# 发布流水线传 SAKURAMEDIA_BUNDLED_PLUGINS_REFRESH=<本次 tag>，让每次发版都重新读取
# 市场 index。
FROM python:3.10-slim-bookworm@sha256:68d914ec641a0b69267ce65184d000a2bc3a9ee2590ab702b82250ab2385735a AS bundled
ARG SAKURAMEDIA_BUNDLED_PLUGINS=0
ARG SAKURAMEDIA_BUNDLED_PLUGIN_PINS=""
ARG SAKURAMEDIA_BUNDLED_PLUGINS_REFRESH=""
COPY docker/backend/fetch_bundled_plugins.py /opt/bundled/fetch_bundled_plugins.py
# token 通过 BuildKit secret 传入，不进镜像层也不进 docker history；index 是公开静态文件，
# 只有 pin 走 GitHub Release API 时才需要，缺失时回退匿名请求。
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

# ── Stage 4: GFriends Filetree 构建期 seed ──
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

# 运行时不内置 ffmpeg 可执行文件：宿主探测/封面与各 media provider 的 create_clip
# 都走 PyAV（插件运行时从宿主 site-packages import av），不再需要 ffmpeg CLI。

# ── Stage 5: 运行时镜像 ──
FROM python:3.10-slim-bookworm@sha256:68d914ec641a0b69267ce65184d000a2bc3a9ee2590ab702b82250ab2385735a AS runtime
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PUID=1000 \
    PGID=1000

# api/aps 是长驻进程，用 jemalloc 覆盖 glibc malloc，抑制长期运行后的堆碎片。
# 只装运行时库（依赖仅 libc6）；用 soname 而不是绝对路径，amd64/arm64 都能解析。
RUN apt-get update \
    && apt-get install -y --no-install-recommends libjemalloc2 \
    && rm -rf /var/lib/apt/lists/*
ENV LD_PRELOAD=libjemalloc.so.2
# preload 失败时 ld.so 只打印警告并继续，这里让构建直接失败，避免静默退回 glibc。
RUN python -c "import pathlib; assert 'libjemalloc' in pathlib.Path('/proc/self/maps').read_text(), 'jemalloc not preloaded'"

COPY --from=builder /install/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
COPY --from=builder /install/bin /usr/local/bin

# 保留 dist-info，部分依赖会在运行时通过 importlib.metadata 读取包元数据。
RUN find /usr/local/lib/python3.10/site-packages \
    \( -type d -name "__pycache__" -o \
       -type d -name "tests" -o \
       -type d -name "test" -o \
       -name "*.pyc" -o \
       -name "*.pyo" \) \
    -exec rm -rf {} + 2>/dev/null; \
    true
# 说明：wheel 里的 .so 已由上游 strip 过，runtime 也没有 binutils；这里不再做 strip。

COPY docker/backend/docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh \
    && groupadd --system app \
    && useradd --create-home --shell /bin/bash --gid app app

# 固定 644：apply-restart 以 app 用户运行 supervisorctl，需要读该配置，不能依赖构建上下文的文件权限。
COPY --chmod=644 docker/backend/supervisord.conf /etc/supervisor/supervisord.conf
# GFriends 构建期 seed：位于 /app 下，不会被 /data 运行时卷遮蔽。
COPY --from=gfriends /out /app/metadata-data
# 内置插件包：本地构建为空目录（安装步骤跳过），CI 发布构建为已校验的 ZIP + 索引。
COPY --from=bundled /bundled /app/bundled-plugins
# 编译后的宿主包与运行时数据文件；镜像内不再包含 src 的 .py 源码。
COPY --from=nuitka /build/nuitka-out/src.cpython-310-*.so /app/
COPY --from=nuitka /build/src/i18n/catalogs/*.json /app/src/i18n/catalogs/
COPY --from=nuitka /build/src/api/uvicorn_config.json /app/src/api/uvicorn_config.json
COPY --from=nuitka /build/src/start/migrations/versions.json /app/src/start/migrations/versions.json
COPY docker/backend/cli.py /app/cli.py
RUN chmod -R a+rX /app/src /app/cli.py /app/bundled-plugins

ARG SAKURAMEDIA_BACKEND_VERSION=v0.0.1
ENV SAKURAMEDIA_BACKEND_VERSION=${SAKURAMEDIA_BACKEND_VERSION}

# 主服务只保留自身运行所需的数据目录，嵌入模型由独立服务管理。
VOLUME ["/data"]
EXPOSE 8000 9001
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["start"]
