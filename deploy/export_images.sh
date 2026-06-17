#!/usr/bin/env bash
# 在【源服务器】导出 Docker 镜像，供新服务器 import。
# 用法：bash deploy/export_images.sh [输出目录]
set -euo pipefail

OUT_DIR="${1:-./docker_images_export}"
mkdir -p "$OUT_DIR"

IMAGES=(vimax:v1 ltx2:v1)

echo "将导出以下镜像到 $OUT_DIR ："
printf '  - %s\n' "${IMAGES[@]}"

for img in "${IMAGES[@]}"; do
  if ! docker image inspect "$img" &>/dev/null; then
    echo "警告: 本地不存在镜像 $img，跳过"
    continue
  fi
  safe_name="${img//:/_}"
  tar_path="$OUT_DIR/${safe_name}.tar"
  echo "导出 $img -> $tar_path"
  docker save -o "$tar_path" "$img"
done

echo ""
echo "导出完成。将整个目录拷贝到新服务器后执行："
echo "  bash deploy/import_images.sh $OUT_DIR"
