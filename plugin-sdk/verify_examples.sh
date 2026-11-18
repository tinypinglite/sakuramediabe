#!/usr/bin/env bash
# 用公开宿主镜像校验所有示例；不需要数据库。
set -euo pipefail

cd "$(dirname "$0")"

IMAGE="${1:-tinyping/sakuramediabe:v0.15.7}"

if ! command -v docker >/dev/null 2>&1; then
  echo "未找到 docker；请安装 Docker 或手动在宿主机上校验示例。" >&2
  exit 1
fi

work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

for example in examples/*/; do
  plugin_id="$(basename "$example")"
  [ -f "$example/manifest.json" ] || continue
  echo "==> 校验 ${plugin_id}"
  mkdir -p "${work_dir}/${plugin_id}"
  cp -R "${example}." "${work_dir}/${plugin_id}/"
  docker run --rm \
    -v "${work_dir}/${plugin_id}:/work/${plugin_id}" \
    "$IMAGE" \
    python /app/cli.py plugins check "/work/${plugin_id}"
done

echo "全部示例校验通过。"
