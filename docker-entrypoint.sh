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

bootstrap_data_dirs() {
    mkdir -p \
        "${DATA_ROOT}/config" \
        "${DATA_ROOT}/db" \
        "${DATA_ROOT}/cache/assets" \
        "${DATA_ROOT}/cache/gfriends" \
        "${DATA_ROOT}/indexes" \
        "${DATA_ROOT}/logs"

    if [ ! -f "${DATA_ROOT}/config/config.toml" ]; then
        echo "Error: missing required config file ${DATA_ROOT}/config/config.toml" >&2
        echo "Create it from config.example.toml before starting the container." >&2
        exit 1
    fi

}

run_database_migrations() {
    echo "Running database migrations..."
    # 迁移必须以应用用户执行，避免新建 sqlite 文件落成 root 权限。
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
    run_database_migrations
    bootstrap_default_data

    # 主服务只负责 API 和任务编排，不再处理 JoyTag 推理设备映射。
    id "${APP_USER}" || true

    echo "Starting supervisor..."
    exec "${SUPERVISORD_BIN}" -c "${SUPERVISORD_CONFIG}"
fi

exec "$@"
