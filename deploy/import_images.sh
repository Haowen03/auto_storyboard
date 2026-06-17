#!/usr/bin/env bash
# 在【新服务器】导入 Docker 镜像。
# 用法：bash deploy/import_images.sh [tar 目录]
set -euo pipefail

IN_DIR="${1:-./docker_images_export}"
if [[ ! -d "$IN_DIR" ]]; then
  echo "目录不存在: $IN_DIR"
  exit 1
fi

shopt -s nullglob
tars=("$IN_DIR"/*.tar)
if [[ ${#tars[@]} -eq 0 ]]; then
  echo "未找到 .tar 文件: $IN_DIR"
  exit 1
fi

for tar_path in "${tars[@]}"; do
  echo "导入 $tar_path ..."
  docker load -i "$tar_path"
done

echo "导入完成。查看镜像："
docker images | head -20
