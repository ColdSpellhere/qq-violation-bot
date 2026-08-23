#!/usr/bin/env bash
set -euo pipefail
instance="${1:-}"
case "$instance" in
  carrot|kona) ;;
  *) echo "instance must be carrot or kona" >&2; exit 2 ;;
esac
instance_root="/opt/qq-bots/instances/$instance"
cd "$instance_root/current"
exec .venv/bin/python bot.py
