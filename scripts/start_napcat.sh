#!/usr/bin/env bash
set -euo pipefail

readonly PROJECT_DIR=/opt/qq-violation-bot
readonly ENV_FILE="${QQ_BOT_ENV_FILE:-${PROJECT_DIR}/.env}"
readonly QQ_BINARY="${NAPCAT_QQ_BINARY:-/root/Napcat/opt/QQ/qq}"

read_env_value() {
  local key="$1"
  awk -F= -v key="$key" '
    $1 == key {
      value = substr($0, index($0, "=") + 1)
      gsub(/^[[:space:]\"]+|[[:space:]\"]+$/, "", value)
      print value
      exit
    }
  ' "$ENV_FILE"
}

if [[ ! -r "$ENV_FILE" ]]; then
  echo "NapCat runtime environment is not readable: $ENV_FILE" >&2
  exit 1
fi

BOT_SELF_ID="${BOT_SELF_ID:-$(read_env_value BOT_SELF_ID)}"
if [[ ! "$BOT_SELF_ID" =~ ^[0-9]{5,12}$ ]]; then
  echo "BOT_SELF_ID must be a 5-12 digit QQ number" >&2
  exit 1
fi

cd /root/Napcat
exec /usr/bin/xvfb-run -a "$QQ_BINARY" --no-sandbox -q "$BOT_SELF_ID"
