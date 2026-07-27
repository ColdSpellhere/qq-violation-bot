#!/usr/bin/env bash
set -euo pipefail
cd /opt/qq-violation-bot
.venv/bin/python - <<'PY'
from dotenv import load_dotenv
load_dotenv('/opt/qq-violation-bot/.env')
from plugins.violation_record.db import backup_database
print(backup_database('manual'))
PY
