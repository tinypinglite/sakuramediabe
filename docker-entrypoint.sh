#!/usr/bin/env bash
set -euo pipefail

APP_USER="app"
APP_GROUP="app"

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
        /data/config \
        /data/db \
        /data/cache/assets \
        /data/cache/gfriends \
        /data/indexes \
        /data/logs \
        /data/lib/joytag

    if [ ! -f /data/config/config.toml ]; then
        echo "Error: missing required config file /data/config/config.toml" >&2
        echo "Create it from config.example.toml before starting the container." >&2
        exit 1
    fi

    chown -R "${PUID:-1000}:${PGID:-1000}" /data
}

add_user_to_device_gid_group() {
    local user_name="$1"
    local gid="$2"
    local group_name=""

    if [ -z "$gid" ]; then
        return 0
    fi

    # Already the user's primary group.
    if [ -n "$PGID" ] && [ "$gid" = "$PGID" ]; then
        return 0
    fi

    group_name="$(getent group | awk -F: -v target_gid="$gid" '$3 == target_gid {print $1; exit}')"
    if [ -z "$group_name" ]; then
        group_name="hostgpu-${gid}"
        groupadd -g "$gid" "$group_name" > /dev/null 2>&1 || true
        group_name="$(getent group | awk -F: -v target_gid="$gid" '$3 == target_gid {print $1; exit}')"
    fi

    if [ -n "$group_name" ]; then
        usermod -aG "$group_name" "$user_name"
        echo "Added ${user_name} to device group ${group_name}(gid=${gid})"
    else
        echo "Warning: failed to resolve group for device gid=${gid}" >&2
    fi
}

if [ "${1:-}" = "start" ]; then
    ensure_app_identity
    bootstrap_data_dirs

    # Auto-detect /dev/dri node groups (render/video) and add app user to them.
    detected_gpu_gids=""
    for device_node in /dev/dri/renderD* /dev/dri/card*; do
        [ -e "$device_node" ] || continue
        device_gid="$(stat -c '%g' "$device_node" 2>/dev/null || true)"
        [ -n "$device_gid" ] || continue
        case " $detected_gpu_gids " in
            *" $device_gid "*) continue ;;
        esac
        detected_gpu_gids="${detected_gpu_gids} ${device_gid}"
        add_user_to_device_gid_group "${APP_USER}" "$device_gid"
    done

    id "${APP_USER}" || true
    ls -l /dev/dri 2>/dev/null || true

    echo "Starting supervisor..."
    exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
fi

exec "$@"
