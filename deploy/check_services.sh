#!/usr/bin/env bash
# 检查 Qwen-Image / LTX / VLM 是否可达。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${DEPLOY_ENV:-$SCRIPT_DIR/.env}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a && source "$ENV_FILE" && set +a
fi

QWEN_URL="${QWEN_IMAGE_BASE_URL:-http://127.0.0.1:9000}"
LTX_URL="${LTX_BASE_URL:-http://127.0.0.1:8000}"
VLM_PROVIDER="${VLM_PROVIDER:-gitee}"

ok=0
fail=0

check_http() {
  local name="$1" url="$2"
  if curl -sf --max-time 5 "${url%/}/docs" >/dev/null 2>&1 \
    || curl -sf --max-time 5 "${url%/}/" >/dev/null 2>&1; then
    echo "[OK]   $name  $url"
    ok=$((ok + 1))
  else
    echo "[FAIL] $name  $url  （服务未启动或地址错误）"
    fail=$((fail + 1))
  fi
}

echo "=== 推理服务连通性 ==="
check_http "Qwen-Image" "$QWEN_URL"
check_http "LTX" "$LTX_URL"

echo ""
echo "=== VLM API Key ==="
if [[ "$VLM_PROVIDER" == "gitee" ]]; then
  if [[ -n "${GITEE_AI_API_KEY:-}" ]]; then
    echo "[OK]   GITEE_AI_API_KEY 已设置"
    ok=$((ok + 1))
  else
    echo "[FAIL] GITEE_AI_API_KEY 未设置（在 deploy/.env 中配置）"
    fail=$((fail + 1))
  fi
elif [[ "$VLM_PROVIDER" == "dashscope" ]]; then
  if [[ -n "${DASHSCOPE_API_KEY:-}" ]]; then
    echo "[OK]   DASHSCOPE_API_KEY 已设置"
    ok=$((ok + 1))
  else
    echo "[FAIL] DASHSCOPE_API_KEY 未设置"
    fail=$((fail + 1))
  fi
fi

echo ""
if [[ $fail -eq 0 ]]; then
  echo "全部检查通过，可以运行 auto_storyboard。"
  exit 0
else
  echo "$fail 项未通过，请先完成 deploy/DEPLOY.md 中的对应步骤。"
  exit 1
fi
