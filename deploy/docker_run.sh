#!/usr/bin/env bash
# 在新服务器上创建与 whw 等价的 Docker 容器。
# 用法：
#   cp deploy/env.example deploy/.env && vim deploy/.env
#   bash deploy/docker_run.sh [镜像名] [容器名]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${DEPLOY_ENV:-$SCRIPT_DIR/.env}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a && source "$ENV_FILE" && set +a
fi

IMAGE="${1:-${CLI_IMAGE:-vimax:v1}}"
CONTAINER_NAME="${2:-${CONTAINER_CLI:-whw}}"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-/home/mx}"
MODEL_ROOT="${MODEL_ROOT:-/mnt}"
SHM_SIZE="${SHM_SIZE:-100gb}"

if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
  echo "容器已存在: $CONTAINER_NAME"
  echo "  进入: docker exec -it $CONTAINER_NAME bash"
  echo "  删除重建: docker rm -f $CONTAINER_NAME"
  exit 1
fi

echo "创建容器 $CONTAINER_NAME（镜像 $IMAGE）"
echo "  挂载 $WORKSPACE_ROOT -> $WORKSPACE_ROOT"
echo "  挂载 $MODEL_ROOT -> $MODEL_ROOT"

docker run -d -it \
  --name "$CONTAINER_NAME" \
  --device=/dev/dri \
  --device=/dev/mxcd \
  --group-add video \
  --network=host \
  --security-opt seccomp=unconfined \
  --security-opt apparmor=unconfined \
  --shm-size "$SHM_SIZE" \
  --ulimit memlock=-1 \
  -v "${WORKSPACE_ROOT}:${WORKSPACE_ROOT}" \
  -v "${MODEL_ROOT}:${MODEL_ROOT}" \
  "$IMAGE" /bin/bash

echo "完成。进入容器："
echo "  docker exec -it $CONTAINER_NAME bash"
