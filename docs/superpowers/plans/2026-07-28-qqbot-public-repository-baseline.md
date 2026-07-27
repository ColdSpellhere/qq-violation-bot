# QQ Bot Public Repository Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a sanitized, testable source baseline that can later be published to `ColdSpellhere/qq-violation-bot` without exposing production identifiers, credentials, chat data, or runtime files.

**Architecture:** Runtime identity stays exclusively in the ignored root-owned `.env`. Source code fails closed when `TARGET_GROUP_ID` is absent, the NapCat launcher reads `BOT_SELF_ID` from protected runtime configuration, and a repository scanner compares tracked content against the live secret values without printing those values.

**Tech Stack:** Python 3.10, `unittest`, python-dotenv, Bash, Git

---

## Execution Boundary

Run this plan before the evidence or operations plans. Do not restart either
production service during this plan. Do not stage all files until the public-tree
scanner passes. The existing `.env` is read-only throughout this plan.

## File Map

- Create: `tests/__init__.py` - test package marker.
- Create: `tests/test_public_source.py` - regression checks for production values and hard-coded identifiers.
- Create: `scripts/check_public_tree.py` - tracked-tree and full-history secret scanner.
- Modify: `plugins/violation_record/config.py` - require exactly one runtime target group.
- Modify: `scripts/start_napcat.sh` - load and validate the bot QQ ID from `.env`.
- Modify: `.env.example` - synthetic public configuration only.
- Modify: `README.md` - remove production recovery details and real identifiers.
- Existing: `.gitignore` - already committed; validate rather than broaden it speculatively.

### Task 0: Preserve the Exact Pre-Change Source and Runtime Configuration

**Files:**
- Backup only: current source, `.env`, and current systemd units

- [ ] **Step 1: Create a root-only pre-work snapshot before editing any source**

```bash
PREWORK_SNAPSHOT="/opt/qq-violation-bot/backups/pre_optimization_$(date +%Y%m%d_%H%M%S)"
install -d -m 700 "$PREWORK_SNAPSHOT"
tar --exclude=.git --exclude=.venv --exclude=data --exclude=backups --exclude=exports --exclude=logs --exclude=evidence --exclude=import_reports --exclude=__pycache__ -czf "$PREWORK_SNAPSHOT/source.tar.gz" -C /opt/qq-violation-bot .
install -m 600 /opt/qq-violation-bot/.env "$PREWORK_SNAPSHOT/.env"
systemctl cat qq-violation-bot.service napcat.service qq-violation-backup.service qq-violation-backup.timer > "$PREWORK_SNAPSHOT/systemd-before.txt"
printf '%s\n' "$PREWORK_SNAPSHOT" > /opt/qq-violation-bot/backups/prework_latest.path
chmod 600 /opt/qq-violation-bot/backups/prework_latest.path
```

Expected: the exact snapshot path exists under the ignored `backups/` directory;
`source.tar.gz` is readable; `.env` and the path marker are mode 600. No service is
restarted.

- [ ] **Step 2: Verify the archive contains the protected source files**

```bash
PREWORK_SNAPSHOT=$(cat /opt/qq-violation-bot/backups/prework_latest.path)
test -d "$PREWORK_SNAPSHOT"
tar -tzf "$PREWORK_SNAPSHOT/source.tar.gz" | sort | awk '$0 ~ /(^|\/)bot.py$|plugins\/violation_record\/(ai_router|matcher|service)\.py$|scripts\/start_napcat\.sh$/ {print}'
```

Expected: `bot.py`, `ai_router.py`, `matcher.py`, `service.py`, and
`start_napcat.sh` are listed. Keep this snapshot for rollback through all plans.

### Task 1: Add Failing Public-Source Boundary Tests

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_public_source.py`

- [ ] **Step 1: Create the package marker**

Create an empty `tests/__init__.py`.

- [ ] **Step 2: Write the production-value and literal-identifier tests**

Create `tests/test_public_source.py` with this complete content:

```python
from __future__ import annotations

import re
import unittest
from pathlib import Path

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FILES = (
    ROOT / ".env.example",
    ROOT / "README.md",
    ROOT / "plugins/violation_record/config.py",
    ROOT / "scripts/start_napcat.sh",
)
SENSITIVE_KEYS = (
    "TARGET_GROUP_ID",
    "BOT_SELF_ID",
    "NAPCAT_ACCESS_TOKEN",
    "AI_API_KEY",
    "ADMIN_SEED",
)


def _public_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in PUBLIC_FILES)


class PublicSourceBoundaryTests(unittest.TestCase):
    def test_live_runtime_values_are_absent_from_public_files(self) -> None:
        env_path = ROOT / ".env"
        if not env_path.exists():
            self.skipTest("production .env is not present")
        public_text = _public_text()
        values = dotenv_values(env_path)
        leaked = [
            key
            for key in SENSITIVE_KEYS
            if str(values.get(key) or "").strip()
            and str(values[key]).strip() in public_text
        ]
        self.assertEqual([], leaked, f"runtime values leaked for keys: {leaked}")

    def test_napcat_launcher_has_no_literal_bot_qq(self) -> None:
        text = (ROOT / "scripts/start_napcat.sh").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"(?:^|\s)-q\s+\d{5,12}(?:\s|$)", text))

    def test_config_has_no_numeric_target_group_fallback(self) -> None:
        text = (ROOT / "plugins/violation_record/config.py").read_text(
            encoding="utf-8"
        )
        self.assertIsNone(re.search(r"values\s*=\s*\[\d{5,12}\]", text))

    def test_public_example_uses_synthetic_values(self) -> None:
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("TARGET_GROUP_ID=123456789", text)
        self.assertIn("NAPCAT_ACCESS_TOKEN=replace-with-random-token", text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the tests and verify the current source fails**

Run:

```bash
cd /opt/qq-violation-bot
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest tests.test_public_source -v
```

Expected: at least the runtime-value, literal launcher QQ, numeric fallback, and
synthetic-example tests fail. Stop if they unexpectedly pass; re-check that the
tests are reading `/opt/qq-violation-bot` and the live `.env`.

### Task 2: Remove Production Identity from Public Source

**Files:**
- Modify: `plugins/violation_record/config.py:21-50`
- Modify: `scripts/start_napcat.sh:1-4`
- Modify: `.env.example`
- Modify: `README.md`
- Test: `tests/test_public_source.py`

- [ ] **Step 1: Replace multi-group fallback parsing with one required target**

In `plugins/violation_record/config.py`, replace `_group_ids_env()` and the
`_ALLOWED_GROUP_IDS` assignment with:

```python
def _target_group_id_env() -> int:
    raw = str(os.getenv("TARGET_GROUP_ID") or "").strip()
    if not raw.isdigit():
        raise RuntimeError("TARGET_GROUP_ID must be one numeric QQ group ID")
    group_id = int(raw)
    if group_id <= 0:
        raise RuntimeError("TARGET_GROUP_ID must be a positive QQ group ID")
    return group_id


_TARGET_GROUP_ID = _target_group_id_env()
```

Then replace the first two fields of `AppConfig` with:

```python
    allowed_group_ids: tuple[int, ...] = (_TARGET_GROUP_ID,)
    target_group_id: int = _TARGET_GROUP_ID
```

Do not change any NLP, query, validation, state, or database constant.

- [ ] **Step 2: Make the NapCat launcher read the ignored runtime file**

Replace `scripts/start_napcat.sh` with:

```bash
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
```

This script reads but never sources `.env`, so arbitrary shell syntax in the file
is not executed.

- [ ] **Step 3: Replace `.env.example` with synthetic runtime values**

Use this complete public example:

```dotenv
DRIVER=~fastapi
HOST=127.0.0.1
PORT=6199
LOG_LEVEL=WARNING
COMMAND_START=["/", ""]
NICKNAME=["违规记录助手"]
SUPERUSERS=[]

TARGET_GROUP_ID=123456789
BOT_SELF_ID=1234567890
NAPCAT_ACCESS_TOKEN=replace-with-random-token

DATABASE_URL=sqlite:////opt/qq-violation-bot/data/violation_records.db

AI_BASE_URL=https://api.deepseek.com
AI_API_KEY=
AI_MODEL=deepseek-chat
AI_TIMEOUT=30

# Format: QQ:Nickname:alias1|alias2;QQ:Nickname
ADMIN_SEED=
```

Do not modify the real `.env`.

- [ ] **Step 4: Sanitize README examples without changing feature semantics**

Make these exact documentation changes:

```text
Delete the sentence that says the current machine recovered .env from
/opt/deepseek_api_key.env.

Replace every concrete TARGET_GROUP_ID/ALLOWED_GROUP_IDS example with:
TARGET_GROUP_ID=123456789

Remove ALLOWED_GROUP_IDS from setup and troubleshooting text because only one
target group is supported.

Replace BOT_SELF_ID and NAPCAT_ACCESS_TOKEN examples with:
BOT_SELF_ID=1234567890
NAPCAT_ACCESS_TOKEN=replace-with-random-token
```

Keep all existing NLP examples and business descriptions unchanged in this task.

- [ ] **Step 5: Run focused tests**

```bash
cd /opt/qq-violation-bot
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest tests.test_public_source -v
```

Expected: `Ran 4 tests` and `OK`.

- [ ] **Step 6: Validate the launcher without starting NapCat**

```bash
bash -n scripts/start_napcat.sh
BOT_SELF_ID=1234567890 QQ_BOT_ENV_FILE=.env.example NAPCAT_QQ_BINARY=/usr/bin/true bash scripts/start_napcat.sh
```

Expected: both commands exit 0 without starting QQ. Do not run
`systemctl restart napcat.service` in this plan.

- [ ] **Step 7: Validate config fail-closed and configured imports**

```bash
env -u TARGET_GROUP_ID .venv/bin/python -c 'import plugins.violation_record.config'
TARGET_GROUP_ID=123456789 .venv/bin/python -c 'from plugins.violation_record.config import CONFIG; assert CONFIG.target_group_id == 123456789; assert CONFIG.allowed_group_ids == (123456789,)'
```

Expected: the first command exits nonzero with the explicit
`TARGET_GROUP_ID must be one numeric QQ group ID` error; the second exits 0.

- [ ] **Step 8: Commit the sanitation change**

```bash
git add .env.example README.md plugins/violation_record/config.py scripts/start_napcat.sh tests/__init__.py tests/test_public_source.py
git diff --cached --check
git commit -m "chore: remove production identity from source"
```

### Task 3: Add a Reusable Git Secret Scanner

**Files:**
- Create: `scripts/check_public_tree.py`
- Create: `tests/test_public_scanner.py`

- [ ] **Step 1: Write the failing scanner tests**

Create `tests/test_public_scanner.py`:

```python
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.check_public_tree import generic_findings, runtime_findings


class PublicScannerTests(unittest.TestCase):
    def test_generic_token_and_private_key_are_detected(self) -> None:
        text = "API_KEY=" + "sk-" + ("a" * 26) + "\nBEGIN " + "OPENSSH PRIVATE KEY"
        findings = generic_findings("fixture.txt", text)
        self.assertEqual(
            ["fixture.txt: generic API token", "fixture.txt: private key material"],
            findings,
        )

    def test_runtime_value_is_reported_by_key_without_printing_value(self) -> None:
        findings = runtime_findings(
            "fixture.py",
            "group = 123456789",
            {"TARGET_GROUP_ID": "123456789"},
        )
        self.assertEqual(["fixture.py: runtime value for TARGET_GROUP_ID"], findings)

    def test_empty_runtime_values_are_ignored(self) -> None:
        self.assertEqual([], runtime_findings("fixture.py", "", {"AI_API_KEY": ""}))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the scanner tests and verify they fail**

```bash
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest tests.test_public_scanner -v
```

Expected: import failure because `scripts.check_public_tree` does not exist.

- [ ] **Step 3: Implement the tracked-tree and history scanner**

Create `scripts/check_public_tree.py`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path
from typing import Iterable

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
SENSITIVE_KEYS = (
    "TARGET_GROUP_ID",
    "BOT_SELF_ID",
    "NAPCAT_ACCESS_TOKEN",
    "AI_API_KEY",
    "ADMIN_SEED",
)
TOKEN_RE = re.compile(r"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,})")
PRIVATE_KEY_RE = re.compile(r"BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY")


def _git(*args: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=text,
    )


def generic_findings(path: str, text: str) -> list[str]:
    findings: list[str] = []
    if TOKEN_RE.search(text):
        findings.append(f"{path}: generic API token")
    if PRIVATE_KEY_RE.search(text):
        findings.append(f"{path}: private key material")
    return findings


def runtime_findings(path: str, text: str, values: dict[str, str]) -> list[str]:
    return [
        f"{path}: runtime value for {key}"
        for key, value in values.items()
        if value and value in text
    ]


def _runtime_values() -> dict[str, str]:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return {}
    parsed = dotenv_values(env_path)
    return {
        key: str(parsed.get(key) or "").strip()
        for key in SENSITIVE_KEYS
        if str(parsed.get(key) or "").strip()
    }


def _tracked_paths(ref: str | None = None) -> list[str]:
    if ref:
        output = _git("ls-tree", "-r", "--name-only", ref).stdout
    else:
        output = _git("ls-files").stdout
    return [line for line in output.splitlines() if line]


def _text_at(path: str, ref: str | None = None) -> str | None:
    if ref:
        raw = _git("show", f"{ref}:{path}", text=False).stdout
    else:
        raw = (ROOT / path).read_bytes()
    if b"\x00" in raw:
        return None
    return raw.decode("utf-8", errors="replace")


def scan_ref(ref: str | None, runtime_values: dict[str, str]) -> list[str]:
    findings: list[str] = []
    prefix = f"{ref}:" if ref else ""
    for path in _tracked_paths(ref):
        text = _text_at(path, ref)
        if text is None:
            continue
        label = f"{prefix}{path}"
        findings.extend(generic_findings(label, text))
        findings.extend(runtime_findings(label, text, runtime_values))
    return findings


def revisions() -> Iterable[str]:
    return _git("rev-list", "--all").stdout.splitlines()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    runtime_values = _runtime_values()
    findings = scan_ref(None, runtime_values)
    if args.history:
        for revision in revisions():
            findings.extend(scan_ref(revision, runtime_values))
    unique = list(dict.fromkeys(findings))
    for finding in unique:
        print(finding)
    if unique:
        print(f"public source scan: FAIL ({len(unique)} findings)")
        return 1
    print("public source scan: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the focused scanner tests**

```bash
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest tests.test_public_scanner -v
```

Expected: `Ran 3 tests` and `OK`.

- [ ] **Step 5: Stage the scanner and run it against the staged tree**

```bash
git add scripts/check_public_tree.py tests/test_public_scanner.py
TARGET_GROUP_ID=123456789 .venv/bin/python scripts/check_public_tree.py
git diff --cached --check
```

Expected: `public source scan: PASS` and no diff errors.

- [ ] **Step 6: Commit the scanner**

```bash
git commit -m "test: add public repository secret scanner"
```

### Task 4: Commit the Sanitized Source Baseline

**Files:**
- Track: `bot.py`
- Track: `plugins/**/*.py`
- Track: `requirements.txt`
- Track: remaining safe `scripts/*`
- Track: `.env.example`, `README.md`, `tests/**/*.py`

- [ ] **Step 1: Run all tests before staging the baseline**

```bash
cd /opt/qq-violation-bot
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest discover -s tests -v
```

Expected: all tests pass. The exact count is the sum of the four boundary tests
and three scanner tests added above.

- [ ] **Step 2: Run static validation**

```bash
TARGET_GROUP_ID=123456789 .venv/bin/python -m compileall -q bot.py plugins scripts tests
bash -n scripts/backup_db.sh scripts/start_bot.sh scripts/start_napcat.sh
.venv/bin/pip check
```

Expected: all commands exit 0 and `pip check` reports no broken requirements.

- [ ] **Step 3: Stage only public source paths**

```bash
git add .env.example README.md bot.py plugins requirements.txt scripts tests
git status --short
```

Expected: `.env`, `.venv`, `data`, `backups`, `exports`, `logs`, `evidence`,
`import_reports`, and caches are absent from the staged list.

- [ ] **Step 4: Prove ignored runtime paths remain ignored**

```bash
git check-ignore -v .env data/violation_records.db backups/example.sqlite3 exports/example.xlsx logs/example.log evidence/images/example.jpg import_reports/example.json .venv/bin/python
```

Expected: each path is matched by `.gitignore`.

- [ ] **Step 5: Scan the complete staged tree and history**

```bash
TARGET_GROUP_ID=123456789 .venv/bin/python scripts/check_public_tree.py --history
git diff --cached --check
```

Expected: `public source scan: PASS` and no whitespace errors. Stop immediately if
any finding appears; do not commit or push.

- [ ] **Step 6: Review the staged diff without exposing `.env`**

```bash
git diff --cached --stat
git diff --cached --name-only
```

Expected: only source, documentation, tests, and scripts listed in the file map.

- [ ] **Step 7: Commit the sanitized source baseline**

```bash
git commit -m "chore: add sanitized application baseline"
```

- [ ] **Step 8: Verify services were not changed or restarted**

```bash
systemctl is-active qq-violation-bot.service napcat.service
systemctl show qq-violation-bot.service napcat.service -p ActiveEnterTimestamp -p NRestarts
git status --short --branch
```

Expected: both services are `active`; their activation timestamps are unchanged
from the pre-plan snapshot; the Git worktree is clean.

## Plan 1 Completion Gate

Proceed to the target-chat/evidence plan only when all tests pass, the full-history
scanner reports `PASS`, and the production services have not been restarted.
