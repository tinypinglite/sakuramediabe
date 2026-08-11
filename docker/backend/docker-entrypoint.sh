#!/usr/bin/env bash
set -euo pipefail

APP_USER="app"
APP_GROUP="app"
DATA_ROOT="${SAKURAMEDIA_DATA_ROOT:-/data}"
APP_ROOT="${SAKURAMEDIA_APP_ROOT:-/app}"
SUPERVISORD_BIN="${SAKURAMEDIA_SUPERVISORD_BIN:-/usr/bin/supervisord}"
SUPERVISORD_CONFIG="${SAKURAMEDIA_SUPERVISORD_CONFIG:-/etc/supervisor/supervisord.conf}"
PYTHON_BIN="${SAKURAMEDIA_PYTHON_BIN:-python}"

ensure_app_identity() {
    local target_uid="${PUID:-1000}"
    local target_gid="${PGID:-1000}"
    local existing_group_name=""

    if ! id "${APP_USER}" >/dev/null 2>&1; then
        useradd --create-home --shell /bin/bash "${APP_USER}"
    fi

    existing_group_name="$(getent group "${target_gid}" | cut -d: -f1 || true)"
    if [ -n "${existing_group_name}" ]; then
        usermod -g "${existing_group_name}" "${APP_USER}"
    else
        groupmod -o -g "${target_gid}" "${APP_GROUP}"
        usermod -g "${target_gid}" "${APP_USER}"
    fi

    usermod -o -u "${target_uid}" "${APP_USER}"
}

chown_if_mismatch() {
    # 目录属主已经是目标 UID/GID 时直接返回，正常重启零开销；只在不匹配时 chown，且仅改 inode 本身。
    local target_uid="$1"
    local target_gid="$2"
    local path="$3"
    [ -e "${path}" ] || return 0
    local cur_uid cur_gid
    cur_uid="$(stat -c '%u' "${path}")"
    cur_gid="$(stat -c '%g' "${path}")"
    if [ "${cur_uid}" != "${target_uid}" ] || [ "${cur_gid}" != "${target_gid}" ]; then
        chown "${target_uid}:${target_gid}" "${path}" || true
    fi
}

bootstrap_data_dirs() {
    # cache/subtitles 是 legacy 目录：字幕已统一到 cache/assets/movies/<shard>/<番号>/subtitles/。
    # 这里仍然建目录并归属，是因为 migrate-movie-subtitles CLI 迁移后要 unlink 旧目录里已搬走的字幕文件，需要写权限。
    mkdir -p \
        "${DATA_ROOT}/config" \
        "${DATA_ROOT}/cache/assets" \
        "${DATA_ROOT}/cache/subtitles" \
        "${DATA_ROOT}/cache/gfriends" \
        "${DATA_ROOT}/media-clips" \
        "${DATA_ROOT}/plugins" \
        "${DATA_ROOT}/logs"

    # bind mount 上来的 /data 可能属主是 root 或宿主机用户，导致切到 app 用户后写不进 config.toml/日志。
    # 只把我们自己 mkdir 的目录节点归给 app，非递归——volume 里的历史缓存/媒体文件保持原样，避免海量文件被扫。
    # 顶层 ${DATA_ROOT} 是用户挂载点，此处不动；app 用户只需要在下列子目录内新建文件即可。
    local target_uid="${PUID:-1000}"
    local target_gid="${PGID:-1000}"
    local dir
    for dir in \
        "${DATA_ROOT}/config" \
        "${DATA_ROOT}/cache" \
        "${DATA_ROOT}/cache/assets" \
        "${DATA_ROOT}/cache/subtitles" \
        "${DATA_ROOT}/cache/gfriends" \
        "${DATA_ROOT}/media-clips" \
        "${DATA_ROOT}/plugins" \
        "${DATA_ROOT}/logs"; do
        chown_if_mismatch "${target_uid}" "${target_gid}" "${dir}"
    done
}

wait_for_database() {
    echo "Waiting for database to become ready..."
    # 显式等待 PostgreSQL 可连接：宿主机重启时容器间无启动顺序保证，避免应用先于数据库就绪导致迁移失败。
    su -s /bin/bash -c "cd \"${APP_ROOT}\" && PYTHONPATH=\"${APP_ROOT}\" \"${PYTHON_BIN}\" -m src.start.commands wait-db --timeout 120" "${APP_USER}"
}

run_database_migrations() {
    echo "Running database migrations..."
    # 迁移必须以应用用户执行，保持运行期文件与日志权限一致。
    su -s /bin/bash -c "cd \"${APP_ROOT}\" && PYTHONPATH=\"${APP_ROOT}\" \"${PYTHON_BIN}\" -m src.start.commands migrate" "${APP_USER}"
}

bootstrap_default_data() {
    echo "Bootstrapping default account and system playlists..."
    # 默认数据初始化保持幂等，首装补齐账号/系统播放列表，老库重复执行会自动跳过。
    su -s /bin/bash -c "cd \"${APP_ROOT}\" && PYTHONPATH=\"${APP_ROOT}\" \"${PYTHON_BIN}\" -m src.start.commands initdb" "${APP_USER}"
}

if [ "${1:-}" = "start" ]; then
    ensure_app_identity
    bootstrap_data_dirs
    wait_for_database
    run_database_migrations
    bootstrap_default_data

    # 主服务只负责 API 和任务编排，不再处理 JoyTag 推理设备映射。
    id "${APP_USER}" || true

    echo "Starting supervisor..."
    exec "${SUPERVISORD_BIN}" -c "${SUPERVISORD_CONFIG}"
fi

exec "$@"
