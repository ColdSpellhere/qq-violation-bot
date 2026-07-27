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
