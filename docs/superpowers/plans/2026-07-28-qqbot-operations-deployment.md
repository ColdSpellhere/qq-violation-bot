# QQ Bot Operations and Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace unsafe live-file backup, contain the NapCat/QQ/Xvfb descriptor leak with measured restarts, deploy the approved application changes with rollback, and publish the sanitized repository to GitHub.

**Architecture:** SQLite uses its online backup API and validates a temporary destination before atomic promotion. A standalone stdlib watchdog reads only the NapCat systemd cgroup, persists cooldown/WebSocket state outside the repository, and restarts only NapCat. Deployment is gated by source/config/unit/database snapshots and staged health checks.

**Tech Stack:** Python 3.10, SQLite, systemd, procfs, `ss`, Git, GitHub SSH deploy key

---

## Execution Boundary

Run after Plans 1 and 2 pass. This plan is the first one allowed to restart
production services. Keep the SSH ControlMaster open. Never delete bound evidence,
chat archives, the business database, or formal backups. Stop on a failed database
integrity check, failed offline test, failed systemd verification, or failed secret
scan.

## File Map

- Modify: `plugins/violation_record/db.py` - SQLite online backup.
- Create: `tests/test_online_backup.py` - backup integrity and failure isolation.
- Create: `scripts/napcat_watchdog.py` - metrics, decision, cooldown, restart, post-check.
- Create: `tests/test_napcat_watchdog.py` - pure threshold and cooldown tests.
- Create: `deploy/systemd/qqbot-napcat-watchdog.service`
- Create: `deploy/systemd/qqbot-napcat-watchdog.timer`
- Create: `deploy/systemd/qqbot-napcat-daily-restart.service`
- Create: `deploy/systemd/qqbot-napcat-daily-restart.timer`
- Modify: `README.md` - operations and rollback commands.
- Runtime modify: `.env`, `/etc/systemd/system/*`, `/root/.ssh/qq-violation-bot-github`, repository-local Git config.

### Task 1: Replace Live SQLite Copy with Verified Online Backup

**Files:**
- Modify: `plugins/violation_record/db.py:1-27`
- Create: `tests/test_online_backup.py`

- [ ] **Step 1: Write failing online-backup tests**

Create `tests/test_online_backup.py`:

```python
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from plugins.violation_record import db


class OnlineBackupTests(unittest.TestCase):
    def test_backup_is_integral_and_contains_committed_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            backups = root / "backups"
            with sqlite3.connect(source) as conn:
                conn.execute("CREATE TABLE records(value TEXT NOT NULL)")
                conn.execute("INSERT INTO records VALUES('kept')")
            config = replace(
                db.CONFIG,
                database_path=source,
                database_url=f"sqlite:///{source}",
            )
            with patch.object(db, "CONFIG", config), patch.object(db, "BACKUP_DIR", backups):
                destination = db.backup_database("test")
            self.assertIsNotNone(destination)
            self.assertFalse(any(backups.glob("*.part")))
            with sqlite3.connect(destination) as conn:
                self.assertEqual("ok", conn.execute("PRAGMA integrity_check").fetchone()[0])
                self.assertEqual("kept", conn.execute("SELECT value FROM records").fetchone()[0])

    def test_missing_source_returns_none_without_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "missing.db"
            backups = root / "backups"
            config = replace(db.CONFIG, database_path=source, database_url=f"sqlite:///{source}")
            with patch.object(db, "CONFIG", config), patch.object(db, "BACKUP_DIR", backups):
                self.assertIsNone(db.backup_database("test"))
            self.assertFalse(backups.exists())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the backup tests and verify the copy implementation is exposed**

```bash
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest tests.test_online_backup -v
```

Expected: the row test may pass with the old copy, but the implementation review
still shows `shutil.copy2`; add the third test below before changing code.

- [ ] **Step 3: Add a test that requires `sqlite3.Connection.backup()`**

Add this method to `OnlineBackupTests`:

```python
    def test_uses_sqlite_online_backup_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            backups = root / "backups"
            real_connect = sqlite3.connect
            with real_connect(source) as conn:
                conn.execute("CREATE TABLE records(value TEXT NOT NULL)")
            backup_calls: list[tuple[object, object]] = []

            class ConnectionProxy:
                def __init__(self, inner):
                    self.inner = inner

                def __enter__(self):
                    self.inner.__enter__()
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    return self.inner.__exit__(exc_type, exc, traceback)

                def __getattr__(self, name):
                    return getattr(self.inner, name)

                def backup(self, target):
                    target_connection = target.inner if isinstance(target, ConnectionProxy) else target
                    backup_calls.append((self.inner, target_connection))
                    return self.inner.backup(target_connection)

            def tracked_connect(*args, **kwargs):
                return ConnectionProxy(real_connect(*args, **kwargs))

            config = replace(db.CONFIG, database_path=source, database_url=f"sqlite:///{source}")
            with (
                patch.object(db, "CONFIG", config),
                patch.object(db, "BACKUP_DIR", backups),
                patch.object(db.sqlite3, "connect", side_effect=tracked_connect),
            ):
                db.backup_database("api")
            self.assertEqual(1, len(backup_calls))
```

Run it against the current code; it must fail because `shutil.copy2` never calls
the proxy's `backup()` method.

- [ ] **Step 4: Implement online backup with atomic validation**

Remove `import shutil`, add `import os`, and replace `backup_database()` with:

```python
def backup_database(reason: str = "manual") -> Path | None:
    ensure_dirs()
    source_path = CONFIG.database_path
    if not source_path.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    destination = BACKUP_DIR / f"db_backup_{reason}_{compact_time()}.sqlite3"
    temporary = destination.with_suffix(destination.suffix + f".{os.getpid()}.part")
    try:
        temporary.unlink(missing_ok=True)
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with sqlite3.connect(source_path) as source, sqlite3.connect(temporary) as target:
            source.backup(target)
        with sqlite3.connect(temporary) as check:
            result = check.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise sqlite3.DatabaseError(f"backup integrity_check returned {result!r}")
        temporary.chmod(0o600)
        temporary.replace(destination)
        return destination
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
```

Do not change `connect()`, schema migration, or any business table.

- [ ] **Step 5: Run focused and full database tests**

```bash
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest tests.test_online_backup tests.test_query_contract -v
```

Expected: all tests pass, including the explicit `backup()` call assertion.

- [ ] **Step 6: Commit online backup**

```bash
git add plugins/violation_record/db.py tests/test_online_backup.py
git diff --cached --check
git commit -m "fix: use verified sqlite online backups"
```

### Task 2: Implement and Test Watchdog Decisions

**Files:**
- Create: `scripts/napcat_watchdog.py`
- Create: `tests/test_napcat_watchdog.py`

- [ ] **Step 1: Write failing pure decision tests**

Create `tests/test_napcat_watchdog.py`:

```python
from __future__ import annotations

import unittest

from scripts.napcat_watchdog import Metrics, State, decide


HEALTHY = Metrics(
    qq_fd_max=100,
    maps_fd_max=0,
    xvfb_fd_max=40,
    maximum_clients=False,
    websocket_established=True,
)


class WatchdogDecisionTests(unittest.TestCase):
    def test_healthy_metrics_do_not_restart(self) -> None:
        decision = decide(HEALTHY, State(), now_epoch=10_000)
        self.assertFalse(decision.restart)
        self.assertEqual(0, decision.next_state.websocket_failures)

    def test_each_resource_threshold_restarts(self) -> None:
        for metrics in (
            Metrics(1500, 0, 40, False, True),
            Metrics(100, 1000, 40, False, True),
            Metrics(100, 0, 220, False, True),
            Metrics(100, 0, 40, True, True),
        ):
            with self.subTest(metrics=metrics):
                self.assertTrue(decide(metrics, State(), 10_000).restart)

    def test_websocket_requires_two_consecutive_failures(self) -> None:
        down = Metrics(100, 0, 40, False, False)
        first = decide(down, State(), 10_000)
        second = decide(down, first.next_state, 10_300)
        self.assertFalse(first.restart)
        self.assertTrue(second.restart)

    def test_cooldown_suppresses_restart(self) -> None:
        leaking = Metrics(1500, 1000, 220, True, True)
        state = State(last_restart_epoch=9_900)
        decision = decide(leaking, state, 10_000)
        self.assertFalse(decision.restart)
        self.assertTrue(decision.cooldown_active)

    def test_scheduled_restart_is_requested_when_not_in_cooldown(self) -> None:
        decision = decide(HEALTHY, State(), 10_000, scheduled=True)
        self.assertTrue(decision.restart)
        self.assertIn("scheduled", decision.reasons)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests and verify the module is missing**

```bash
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest tests.test_napcat_watchdog -v
```

Expected: import failure for `scripts.napcat_watchdog`.

- [ ] **Step 3: Implement immutable metrics, state, and decision types**

Start `scripts/napcat_watchdog.py` with:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path


QQ_FD_LIMIT = 1500
MAPS_FD_LIMIT = 1000
XVFB_FD_LIMIT = 220
COOLDOWN_SECONDS = 30 * 60
STATE_PATH = Path("/var/lib/qq-violation-bot/watchdog-state.json")
LOCK_PATH = Path("/run/lock/qqbot-napcat-watchdog.lock")


@dataclass(frozen=True)
class Metrics:
    qq_fd_max: int
    maps_fd_max: int
    xvfb_fd_max: int
    maximum_clients: bool
    websocket_established: bool


@dataclass(frozen=True)
class State:
    last_restart_epoch: int = 0
    websocket_failures: int = 0


@dataclass(frozen=True)
class Decision:
    restart: bool
    reasons: tuple[str, ...]
    cooldown_active: bool
    next_state: State


def decide(metrics: Metrics, state: State, now_epoch: int, scheduled: bool = False) -> Decision:
    failures = 0 if metrics.websocket_established else state.websocket_failures + 1
    next_state = replace(state, websocket_failures=failures)
    reasons: list[str] = []
    if metrics.qq_fd_max >= QQ_FD_LIMIT:
        reasons.append("qq_fd")
    if metrics.maps_fd_max >= MAPS_FD_LIMIT:
        reasons.append("maps_fd")
    if metrics.xvfb_fd_max >= XVFB_FD_LIMIT:
        reasons.append("xvfb_fd")
    if metrics.maximum_clients:
        reasons.append("maximum_clients")
    if failures >= 2:
        reasons.append("websocket")
    if scheduled:
        reasons.append("scheduled")
    cooldown = bool(state.last_restart_epoch and now_epoch - state.last_restart_epoch < COOLDOWN_SECONDS)
    return Decision(bool(reasons) and not cooldown, tuple(reasons), cooldown, next_state)
```

- [ ] **Step 4: Implement cgroup-scoped metric collection**

Add functions with these exact subprocess boundaries:

```python
def _run(*command: str, check: bool = True) -> str:
    return subprocess.run(command, check=check, capture_output=True, text=True).stdout


def _cgroup_pids() -> list[int]:
    group = _run("systemctl", "show", "napcat.service", "-p", "ControlGroup", "--value").strip()
    if not group or group == "/":
        raise RuntimeError("napcat.service has no dedicated cgroup")
    path = Path("/sys/fs/cgroup") / group.lstrip("/") / "cgroup.procs"
    return [int(line) for line in path.read_text().splitlines() if line.strip().isdigit()]


def _comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def _fd_counts(pid: int) -> tuple[int, int]:
    try:
        targets = [path.resolve(strict=False) for path in Path(f"/proc/{pid}/fd").iterdir()]
    except OSError:
        return 0, 0
    maps = sum(1 for target in targets if str(target) == f"/proc/{pid}/maps")
    return len(targets), maps


def collect_metrics() -> Metrics:
    pids = _cgroup_pids()
    qq_counts = [_fd_counts(pid) for pid in pids if _comm(pid) in {"qq", "node"}]
    xvfb_counts = [_fd_counts(pid)[0] for pid in pids if _comm(pid) == "Xvfb"]
    sockets = _run("ss", "-Htanp", check=False)
    websocket = any("ESTAB" in line and ":6199" in line for line in sockets.splitlines())
    recent = _run(
        "journalctl", "-u", "napcat.service", "--since", "10 minutes ago", "--no-pager", check=False
    )
    return Metrics(
        qq_fd_max=max((total for total, _ in qq_counts), default=0),
        maps_fd_max=max((maps for _, maps in qq_counts), default=0),
        xvfb_fd_max=max(xvfb_counts, default=0),
        maximum_clients="Maximum number of clients reached" in recent,
        websocket_established=websocket,
    )
```

Do not inspect processes outside `/system.slice/napcat.service` for threshold
decisions.

- [ ] **Step 5: Implement state, locking, one restart, and post-check**

Add:

```python
def load_state() -> State:
    try:
        return State(**json.loads(STATE_PATH.read_text(encoding="utf-8")))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return State()


def save_state(state: State) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(asdict(state)), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(STATE_PATH)


def wait_for_recovery(timeout_seconds: int = 90) -> Metrics:
    deadline = time.monotonic() + timeout_seconds
    latest = Metrics(0, 0, 0, False, False)
    while time.monotonic() < deadline:
        try:
            active = _run("systemctl", "is-active", "napcat.service", check=False).strip() == "active"
            bot_active = _run("systemctl", "is-active", "qq-violation-bot.service", check=False).strip() == "active"
            latest = collect_metrics()
            if active and bot_active and latest.websocket_established and latest.qq_fd_max < QQ_FD_LIMIT and latest.maps_fd_max < MAPS_FD_LIMIT and latest.xvfb_fd_max < XVFB_FD_LIMIT:
                return latest
        except (OSError, RuntimeError, ValueError):
            pass
        time.sleep(3)
    raise RuntimeError(f"NapCat post-check failed: {asdict(latest)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with LOCK_PATH.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            now_epoch = int(time.time())
            state = load_state()
            metrics = collect_metrics()
            decision = decide(metrics, state, now_epoch, scheduled=args.scheduled)
            print(json.dumps({"metrics": asdict(metrics), "decision": asdict(decision)}, ensure_ascii=True))
            if args.check_only or not decision.restart:
                save_state(decision.next_state)
                return 0
            subprocess.run(["systemctl", "restart", "napcat.service"], check=True)
            next_state = replace(decision.next_state, last_restart_epoch=now_epoch, websocket_failures=0)
            save_state(next_state)
            recovered = wait_for_recovery()
            print(json.dumps({"post_restart": asdict(recovered)}, ensure_ascii=True))
            return 0
    except BlockingIOError:
        print("watchdog already running")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

No exception other than a competing nonblocking lock is hidden by `main()`.

- [ ] **Step 6: Run tests and check-only collection**

```bash
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest tests.test_napcat_watchdog -v
.venv/bin/python scripts/napcat_watchdog.py --check-only
```

Expected: five tests pass. The check-only command prints one JSON decision, does
not restart NapCat, and reports the currently elevated FD metrics.

- [ ] **Step 7: Commit the watchdog implementation**

```bash
git add scripts/napcat_watchdog.py tests/test_napcat_watchdog.py
git diff --cached --check
git commit -m "feat: add napcat resource watchdog"
```

### Task 3: Add and Validate systemd Units Without Enabling Them

**Files:**
- Create: `deploy/systemd/qqbot-napcat-watchdog.service`
- Create: `deploy/systemd/qqbot-napcat-watchdog.timer`
- Create: `deploy/systemd/qqbot-napcat-daily-restart.service`
- Create: `deploy/systemd/qqbot-napcat-daily-restart.timer`

- [ ] **Step 1: Create the watchdog service**

```ini
[Unit]
Description=QQ Bot NapCat resource watchdog
After=napcat.service qq-violation-bot.service

[Service]
Type=oneshot
WorkingDirectory=/opt/qq-violation-bot
ExecStart=/opt/qq-violation-bot/.venv/bin/python /opt/qq-violation-bot/scripts/napcat_watchdog.py
TimeoutStartSec=150
```

- [ ] **Step 2: Create the five-minute timer**

```ini
[Unit]
Description=Run QQ Bot NapCat watchdog every five minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
AccuracySec=30s
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Create the daily restart service and timer**

`qqbot-napcat-daily-restart.service`:

```ini
[Unit]
Description=QQ Bot scheduled NapCat restart
After=qq-violation-backup.service

[Service]
Type=oneshot
WorkingDirectory=/opt/qq-violation-bot
ExecStart=/opt/qq-violation-bot/.venv/bin/python /opt/qq-violation-bot/scripts/napcat_watchdog.py --scheduled
TimeoutStartSec=150
```

`qqbot-napcat-daily-restart.timer`:

```ini
[Unit]
Description=Restart QQ Bot NapCat daily at 04:10

[Timer]
OnCalendar=*-*-* 04:10:00
Persistent=true
AccuracySec=1min

[Install]
WantedBy=timers.target
```

- [ ] **Step 4: Validate repository unit files**

```bash
systemd-analyze verify deploy/systemd/qqbot-napcat-watchdog.service deploy/systemd/qqbot-napcat-watchdog.timer deploy/systemd/qqbot-napcat-daily-restart.service deploy/systemd/qqbot-napcat-daily-restart.timer
```

Expected: exit 0 with no unit errors. Do not copy or enable units yet.

- [ ] **Step 5: Document watchdog operations and rollback**

Add a README operations section containing these exact commands and meanings:

```bash
systemctl status qqbot-napcat-watchdog.timer qqbot-napcat-daily-restart.timer
.venv/bin/python scripts/napcat_watchdog.py --check-only
journalctl -u qqbot-napcat-watchdog.service --since today --no-pager
systemctl disable --now qqbot-napcat-watchdog.timer qqbot-napcat-daily-restart.timer
```

State the thresholds (`1500`, `1000`, `220`), two-check WebSocket rule, 30-minute
cooldown, daily 04:10 restart, 90-second post-check, and the fact that only
`napcat.service` is restarted.

- [ ] **Step 6: Commit the units and documentation**

```bash
git add deploy/systemd README.md
git commit -m "ops: add napcat watchdog timers"
```

### Task 4: Create Pre-Deployment Snapshots and Run All Offline Gates

**Files:**
- Backup only: source, `.env`, databases, current units

- [ ] **Step 1: Capture current access and service state**

```bash
ssh -S /tmp/codex-qqbot-019fa417.sock -O check ignored.invalid
systemctl is-active qq-violation-bot.service napcat.service
systemctl show qq-violation-bot.service napcat.service -p MainPID -p ActiveEnterTimestamp -p NRestarts
ss -Htanp | awk '$1 == "ESTAB" && $0 ~ /:6199/ {print}'
```

Expected: SSH master running, both services active, and one established OneBot
connection. Stop if access or the connection is already unhealthy.

- [ ] **Step 2: Create a root-only deployment snapshot directory**

```bash
DEPLOY_SNAPSHOT="/opt/qq-violation-bot/backups/deploy_$(date +%Y%m%d_%H%M%S)"
install -d -m 700 "$DEPLOY_SNAPSHOT"
tar --exclude=.git --exclude=.venv --exclude=data --exclude=backups --exclude=exports --exclude=logs --exclude=evidence --exclude=import_reports --exclude=__pycache__ -czf "$DEPLOY_SNAPSHOT/source.tar.gz" -C /opt/qq-violation-bot .
install -m 600 /opt/qq-violation-bot/.env "$DEPLOY_SNAPSHOT/.env"
systemctl cat qq-violation-bot.service napcat.service qq-violation-backup.service qq-violation-backup.timer > "$DEPLOY_SNAPSHOT/systemd-before.txt"
tar -czf "$DEPLOY_SNAPSHOT/napcat-config.tar.gz" -C /root/Napcat/opt/QQ/resources/app/app_launcher/napcat config
printf '%s\n' "$DEPLOY_SNAPSHOT" > /run/qqbot-deploy-snapshot-path
chmod 600 /run/qqbot-deploy-snapshot-path
```

Expected: the snapshot is under the already ignored `backups/` directory, has mode
700, and its exact path is recoverable from `/run/qqbot-deploy-snapshot-path`.

- [ ] **Step 3: Create and validate an online production database backup**

```bash
bash scripts/backup_db.sh
LATEST_BACKUP=$(ls -1t backups/db_backup_manual_*.sqlite3 | head -n 1)
sqlite3 "$LATEST_BACKUP" 'PRAGMA integrity_check;'
sqlite3 data/violation_records.db 'PRAGMA integrity_check;'
```

Expected: both integrity checks print `ok`. Stop otherwise.

- [ ] **Step 4: Run all offline quality gates**

```bash
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest discover -s tests -v
TARGET_GROUP_ID=123456789 .venv/bin/python -m compileall -q bot.py plugins scripts tests
bash -n scripts/backup_db.sh scripts/start_bot.sh scripts/start_napcat.sh
systemd-analyze verify deploy/systemd/*.service deploy/systemd/*.timer
.venv/bin/pip check
TARGET_GROUP_ID=123456789 .venv/bin/python scripts/check_public_tree.py --history
git diff --check
git status --short --branch
```

Expected: every command succeeds, public scan passes, and the Git worktree is clean.

### Task 5: Apply Runtime Settings and Restart the Python Bot Once

**Files:**
- Runtime modify: `/opt/qq-violation-bot/.env`

- [ ] **Step 1: Set approved runtime switches with a structured dotenv writer**

```bash
.venv/bin/python - <<'PY'
from dotenv import set_key

path = "/opt/qq-violation-bot/.env"
for key, value in {
    "LOG_LEVEL": "WARNING",
    "EVIDENCE_REQUIRED": "false",
    "EVIDENCE_MAX_BYTES": "20971520",
    "MUTE_ENABLED": "false",
}.items():
    set_key(path, key, value, quote_mode="never")
PY
```

- [ ] **Step 2: Enforce NapCat warning-only console logging with structured JSON**

```bash
python3 - <<'PY'
import json
import os
from pathlib import Path

root = Path("/root/Napcat/opt/QQ/resources/app/app_launcher/napcat/config")
changed = 0
for path in sorted(root.glob("napcat*.json")):
    data = json.loads(path.read_text(encoding="utf-8"))
    if "fileLog" not in data:
        continue
    data["fileLog"] = False
    data["consoleLog"] = True
    data["consoleLogLevel"] = "warn"
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(path.stat().st_mode & 0o777)
    temporary.replace(path)
    changed += 1
if changed < 1:
    raise SystemExit("no NapCat logging configuration found")
print(f"updated NapCat logging configs: {changed}")
PY
```

Expected: at least one config updated; no access token or account ID is printed.

- [ ] **Step 3: Prove there are no duplicate managed keys**

```bash
for key in LOG_LEVEL EVIDENCE_REQUIRED EVIDENCE_MAX_BYTES MUTE_ENABLED TARGET_GROUP_ID BOT_SELF_ID; do
  count=$(awk -F= -v key="$key" '$1 == key {count++} END {print count+0}' .env)
  test "$count" -eq 1 || { echo "invalid key count: $key=$count"; exit 1; }
done
```

Expected: exit 0. Do not print secret values.

- [ ] **Step 4: Restart only the Python bot to load application changes**

```bash
systemctl restart qq-violation-bot.service
systemctl is-active qq-violation-bot.service
journalctl -u qq-violation-bot.service --since '2 minutes ago' --no-pager -p warning
```

Expected: service active, no startup exception, and no normal chat event lines at
the `WARNING` log level.

- [ ] **Step 5: Verify OneBot reconnects without restarting NapCat**

```bash
timeout 90 bash -c 'until ss -Htanp | awk '\''$1 == "ESTAB" && $0 ~ /:6199/ {found=1} END {exit !found}'\''; do sleep 3; done'
systemctl is-active napcat.service
```

Expected: established connection within 90 seconds and NapCat remains active.

- [ ] **Step 6: Verify database and sidecar ownership boundaries**

```bash
sqlite3 data/violation_records.db 'PRAGMA integrity_check;'
find data evidence -maxdepth 2 -type f -printf '%m %p\n' 2>/dev/null | sort
```

Expected: business integrity `ok`; created archive/evidence databases and evidence
files are mode 600, and evidence directories are mode 700.

### Task 6: Install Timers and Perform the Expected Controlled NapCat Restart

**Files:**
- Install: `/etc/systemd/system/qqbot-napcat-watchdog.service`
- Install: `/etc/systemd/system/qqbot-napcat-watchdog.timer`
- Install: `/etc/systemd/system/qqbot-napcat-daily-restart.service`
- Install: `/etc/systemd/system/qqbot-napcat-daily-restart.timer`

- [ ] **Step 1: Copy units with fixed modes and verify installed paths**

```bash
install -m 0644 deploy/systemd/qqbot-napcat-watchdog.service /etc/systemd/system/qqbot-napcat-watchdog.service
install -m 0644 deploy/systemd/qqbot-napcat-watchdog.timer /etc/systemd/system/qqbot-napcat-watchdog.timer
install -m 0644 deploy/systemd/qqbot-napcat-daily-restart.service /etc/systemd/system/qqbot-napcat-daily-restart.service
install -m 0644 deploy/systemd/qqbot-napcat-daily-restart.timer /etc/systemd/system/qqbot-napcat-daily-restart.timer
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/qqbot-napcat-*.service /etc/systemd/system/qqbot-napcat-*.timer
```

Expected: verification exits 0. Do not enable timers until the next step.

- [ ] **Step 2: Record pre-restart metrics**

```bash
.venv/bin/python scripts/napcat_watchdog.py --check-only
systemctl show napcat.service -p MainPID -p ActiveEnterTimestamp -p NRestarts
```

Expected: current metrics meet at least one threshold, explaining the upcoming
restart.

- [ ] **Step 3: Enable timers and start the watchdog service once**

```bash
systemctl enable --now qqbot-napcat-watchdog.timer qqbot-napcat-daily-restart.timer
systemctl start qqbot-napcat-watchdog.service
```

Expected impact: NapCat/QQ connection is unavailable for approximately 30-90
seconds. SSH and `qq-violation-bot.service` remain active.

- [ ] **Step 4: Verify the post-restart state**

```bash
systemctl is-active qq-violation-bot.service napcat.service qqbot-napcat-watchdog.timer qqbot-napcat-daily-restart.timer
systemctl status qqbot-napcat-watchdog.service --no-pager
.venv/bin/python scripts/napcat_watchdog.py --check-only
ss -Htanp | awk '$1 == "ESTAB" && $0 ~ /:6199/ {print}'
systemctl list-timers qqbot-napcat-watchdog.timer qqbot-napcat-daily-restart.timer --no-pager
```

Expected: all active units are healthy; QQ/Node and Xvfb FD values are below
thresholds; WebSocket established; daily timer next run is 04:10.

### Task 7: Remove Only the Verified Orphan Xvfb

- [ ] **Step 1: Compute active NapCat cgroup PIDs and orphan candidates**

```bash
NAPCAT_CGROUP=$(systemctl show napcat.service -p ControlGroup --value)
mapfile -t ACTIVE_PIDS < "/sys/fs/cgroup${NAPCAT_CGROUP}/cgroup.procs"
mapfile -t XVFB_PIDS < <(pgrep -x Xvfb)
ORPHANS=()
for pid in "${XVFB_PIDS[@]}"; do
  in_service=0
  for active in "${ACTIVE_PIDS[@]}"; do
    [[ "$pid" == "$active" ]] && in_service=1
  done
  [[ "$in_service" -eq 0 ]] && ORPHANS+=("$pid")
done
printf '%s\n' "${ORPHANS[@]}"
test "${#ORPHANS[@]}" -eq 1
printf '%s\n' "${ORPHANS[0]}" > /run/qqbot-orphan-xvfb.pid
chmod 600 /run/qqbot-orphan-xvfb.pid
```

Expected: exactly one candidate. Stop if zero or more than one.

- [ ] **Step 2: Verify the candidate is detached and unused**

```bash
ORPHAN_PID=$(cat /run/qqbot-orphan-xvfb.pid)
[[ "$ORPHAN_PID" =~ ^[0-9]+$ ]]
! grep -qx "$ORPHAN_PID" "/sys/fs/cgroup$(systemctl show napcat.service -p ControlGroup --value)/cgroup.procs"
test "$(ps -o ppid= -p "$ORPHAN_PID" | tr -d ' ')" = "1"
ps -o pid,ppid,lstart,comm,args -p "$ORPHAN_PID"
lsof -nP -a -p "$ORPHAN_PID" -i 2>/dev/null || true
```

Expected: PPID 1, old start time, not in NapCat cgroup, and no relevant network
client. This is the final destructive-action guard.

- [ ] **Step 3: Terminate that exact PID and verify it stays gone**

```bash
ORPHAN_PID=$(cat /run/qqbot-orphan-xvfb.pid)
[[ "$ORPHAN_PID" =~ ^[0-9]+$ ]]
kill -TERM "$ORPHAN_PID"
timeout 10 bash -c 'while kill -0 "$1" 2>/dev/null; do sleep 1; done' _ "$ORPHAN_PID"
rm -f /run/qqbot-orphan-xvfb.pid
systemctl is-active napcat.service qq-violation-bot.service
```

Expected: candidate exits; both production services remain active. No files are
deleted.

### Task 8: Validate Isolation, Persistence, Backup, and Rollback

- [ ] **Step 1: Verify archive database cannot contain another group**

```bash
if test -f data/chat_archive.db; then
  sqlite3 data/chat_archive.db 'SELECT group_id,COUNT(*) FROM chat_messages GROUP BY group_id;'
else
  echo 'chat archive not created yet'
fi
```

Expected: zero rows if no new target message has arrived, or exactly one group ID
equal to the runtime target. Do not print message text.

- [ ] **Step 2: Verify no normal framework event content is entering the journal**

```bash
journalctl -u qq-violation-bot.service --since '10 minutes ago' --no-pager | awk '/\[SUCCESS\]|收到允许群消息|GroupMessageEvent/ {count++} END {print count+0}'
```

Expected: `0`.

- [ ] **Step 3: Run a second verified online backup**

```bash
bash scripts/backup_db.sh
LATEST_BACKUP=$(ls -1t backups/db_backup_manual_*.sqlite3 | head -n 1)
sqlite3 "$LATEST_BACKUP" 'PRAGMA integrity_check;'
```

Expected: `ok`.

- [ ] **Step 4: Record exact rollback commands without executing them**

```bash
DEPLOY_SNAPSHOT=$(cat /run/qqbot-deploy-snapshot-path)
test -d "$DEPLOY_SNAPSHOT"
PREWORK_SNAPSHOT=$(cat /opt/qq-violation-bot/backups/prework_latest.path)
test -d "$PREWORK_SNAPSHOT"
systemctl disable --now qqbot-napcat-watchdog.timer qqbot-napcat-daily-restart.timer
install -m 600 "$PREWORK_SNAPSHOT/.env" /opt/qq-violation-bot/.env
tar -xzf "$PREWORK_SNAPSHOT/source.tar.gz" -C /opt/qq-violation-bot
tar -xzf "$DEPLOY_SNAPSHOT/napcat-config.tar.gz" -C /root/Napcat/opt/QQ/resources/app/app_launcher/napcat
systemctl restart qq-violation-bot.service
systemctl daemon-reload
```

These commands are documentation only at this checkpoint. Do not execute them
while acceptance checks pass.

### Task 9: Publish the Sanitized Public GitHub Repository

**Files:**
- Runtime create: `/root/.ssh/qq-violation-bot-github`
- Runtime modify: repository-local `.git/config`

- [ ] **Step 1: Run final tree and history gates**

```bash
git status --short --branch
TARGET_GROUP_ID=123456789 .venv/bin/python scripts/check_public_tree.py --history
git log --oneline --decorate --stat -12
```

Expected: clean worktree, scanner `PASS`, and no runtime/generated files in any
commit.

- [ ] **Step 2: Create the empty public repository in the authenticated GitHub session**

Create `ColdSpellhere/qq-violation-bot` with visibility `Public`. Do not initialize
it with README, `.gitignore`, license, or sample files because the server already
has history.

- [ ] **Step 3: Generate a repository-specific SSH deploy key on the server**

```bash
install -d -m 700 /root/.ssh
ssh-keygen -t ed25519 -f /root/.ssh/qq-violation-bot-github -N '' -C 'qq-violation-bot deploy key'
chmod 600 /root/.ssh/qq-violation-bot-github
chmod 644 /root/.ssh/qq-violation-bot-github.pub
```

Expected: a new key pair; never display or transmit the private key.

- [ ] **Step 4: Add only the public key as a write-enabled deploy key**

In GitHub repository Settings -> Deploy keys, add the contents of
`/root/.ssh/qq-violation-bot-github.pub`, name it `production qq bot`, and enable
write access. This key is limited to this repository.

- [ ] **Step 5: Verify GitHub host identity and configure repository-local SSH**

Before accepting a new host key, compare GitHub's current published SSH
fingerprints against the observed `ssh-keyscan github.com` output using official
GitHub documentation. Then run:

```bash
git config core.sshCommand 'ssh -i /root/.ssh/qq-violation-bot-github -o IdentitiesOnly=yes'
git remote add origin git@github.com:ColdSpellhere/qq-violation-bot.git
ssh -i /root/.ssh/qq-violation-bot-github -o IdentitiesOnly=yes -T git@github.com 2>&1 | grep -F 'successfully authenticated'
```

Expected: GitHub reports successful authentication for the repository key. If
`origin` already exists, inspect it and use
`git remote set-url origin git@github.com:ColdSpellhere/qq-violation-bot.git` only
when it is not the approved repository.

- [ ] **Step 6: Push main and verify the remote commit**

```bash
git push -u origin main
LOCAL_HEAD=$(git rev-parse HEAD)
REMOTE_HEAD=$(git ls-remote origin refs/heads/main | awk '{print $1}')
test "$LOCAL_HEAD" = "$REMOTE_HEAD"
```

Expected: push succeeds and both hashes match.

- [ ] **Step 7: Inspect the public repository without authentication**

Open `https://github.com/ColdSpellhere/qq-violation-bot` in a logged-out/public
view. Confirm source and docs render, while `.env`, databases, evidence, chat
archives, backups, exports, logs, import reports, and runtime IDs are absent.

### Task 10: Final Acceptance Record

- [ ] **Step 1: Run the complete final verification set**

```bash
systemctl is-active qq-violation-bot.service napcat.service qqbot-napcat-watchdog.timer qqbot-napcat-daily-restart.timer
.venv/bin/python scripts/napcat_watchdog.py --check-only
ss -Htanp | awk '$1 == "ESTAB" && $0 ~ /:6199/ {print}'
sqlite3 data/violation_records.db 'PRAGMA integrity_check;'
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest discover -s tests -v
TARGET_GROUP_ID=123456789 .venv/bin/python scripts/check_public_tree.py --history
git status --short --branch
git ls-remote origin refs/heads/main
```

Expected: active services/timers, healthy resource metrics, established WebSocket,
SQLite `ok`, all tests pass, public scan passes, clean branch, and remote `main`
matches local HEAD.

- [ ] **Step 2: Report residual live-test boundary**

Report separately whether a real target-group command has naturally exercised
archive/evidence delivery after deployment. Do not manufacture a production
violation solely for testing. If no suitable target message occurred, state that
offline integration and service health passed while the first real evidence flow
still requires observational confirmation.

## Plan 3 Completion Gate

The work is complete only when the final verification set passes and the public
repository is independently visible without runtime data. Keep SSH connected for
the user's follow-up work.
