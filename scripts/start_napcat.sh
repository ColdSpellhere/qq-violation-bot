#!/usr/bin/env bash
set -euo pipefail

instance="${1:-}"
case "$instance" in
  carrot) readonly EXPECTED_PORT=6199 ;;
  kona) readonly EXPECTED_PORT=6299 ;;
  *) echo "instance must be carrot or kona" >&2; exit 2 ;;
esac

readonly QQ_BOTS_ROOT="${QQ_BOTS_ROOT:-/opt/qq-bots}"
readonly INSTANCE_ROOT="$QQ_BOTS_ROOT/instances/$instance"
readonly ENV_FILE="$INSTANCE_ROOT/.env"
readonly NAPCAT_INSTALL_ROOT="${NAPCAT_INSTALL_ROOT:-/root/Napcat}"
readonly QQ_BINARY="${NAPCAT_QQ_BINARY:-$NAPCAT_INSTALL_ROOT/opt/QQ/qq}"
readonly XVFB_RUN="${XVFB_RUN:-/usr/bin/xvfb-run}"

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

BOT_SELF_ID="$(read_env_value BOT_SELF_ID)"
NAPCAT_ACCESS_TOKEN="$(read_env_value NAPCAT_ACCESS_TOKEN)"
NAPCAT_REVERSE_WS_PORT="$(read_env_value PORT)"
if [[ ! "$BOT_SELF_ID" =~ ^[0-9]{5,12}$ ]]; then
  echo "BOT_SELF_ID must be a 5-12 digit QQ number" >&2
  exit 1
fi
if [[ -z "$NAPCAT_ACCESS_TOKEN" ]]; then
  echo "NAPCAT_ACCESS_TOKEN must not be empty" >&2
  exit 1
fi
if [[ "$NAPCAT_REVERSE_WS_PORT" != "$EXPECTED_PORT" ]]; then
  echo "$instance must use reverse WebSocket port $EXPECTED_PORT" >&2
  exit 1
fi

export BOT_SELF_ID NAPCAT_ACCESS_TOKEN NAPCAT_REVERSE_WS_PORT
export HOME="$INSTANCE_ROOT/napcat/home"
export XDG_CONFIG_HOME="$INSTANCE_ROOT/napcat/config"
export XDG_DATA_HOME="$INSTANCE_ROOT/napcat/data"
export XDG_CACHE_HOME="$INSTANCE_ROOT/napcat/cache"
readonly QQ_USER_DATA="$INSTANCE_ROOT/napcat/qq-user-data"
install -d -m 0700 "$HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" \
  "$XDG_CACHE_HOME" "$QQ_USER_DATA"

cd "$NAPCAT_INSTALL_ROOT"
exec "$XVFB_RUN" -a "$QQ_BINARY" --no-sandbox \
  "--user-data-dir=$QQ_USER_DATA" -q "$BOT_SELF_ID"
