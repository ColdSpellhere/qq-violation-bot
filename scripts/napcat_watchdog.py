#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

sys.dont_write_bytecode = True
if __package__:
    from .ops_runtime import OPS_VERSION, cgroup_pids, exact_onebot_sockets, onebot_status, read_environment
else:
    from ops_runtime import OPS_VERSION, cgroup_pids, exact_onebot_sockets, onebot_status, read_environment


QQ_FD_LIMIT = 1500
MAPS_FD_LIMIT = 1000
XVFB_FD_LIMIT = 220
COOLDOWN_SECONDS = 30 * 60


@dataclass(frozen=True)
class RuntimeTarget:
    instance: str
    napcat_unit: str
    bot_unit: str
    port: int
    state_path: Path
    lock_path: Path
    instance_root: Path | None = None


def target_for_instance(instance: str, root: Path = Path('/opt/qq-bots')) -> RuntimeTarget:
    ports = {"carrot": 6199, "kona": 6299}
    if instance not in ports:
        raise ValueError("instance must be carrot or kona")
    return RuntimeTarget(
        instance=instance,
        napcat_unit=f"napcat@{instance}.service",
        bot_unit=f"qqbot@{instance}.service",
        port=ports[instance],
        state_path=Path(f"/var/lib/qq-bots/{instance}/watchdog-state.json"),
        lock_path=Path(f"/run/lock/qqbot-napcat-watchdog-{instance}.lock"),
        instance_root=root/'instances'/instance,
    )


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
    return subprocess.run(command, check=check, capture_output=True, text=True, timeout=8).stdout


def _cgroup_pids(target: RuntimeTarget) -> list[int]:
    return sorted(cgroup_pids(target.napcat_unit, _run))


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


def collect_metrics(target: RuntimeTarget) -> Metrics:
    pids = _cgroup_pids(target)
    qq_counts = [_fd_counts(pid) for pid in pids if _comm(pid) in {"qq", "node"}]
    xvfb_counts = [_fd_counts(pid)[0] for pid in pids if _comm(pid) == "Xvfb"]
    sockets = _run("ss", "-Htanp", check=False)
    websocket = exact_onebot_sockets(sockets, target.port, cgroup_pids(target.bot_unit, _run), set(pids))
    if websocket:
        try:
            values = read_environment((target.instance_root or Path('/opt/qq-bots/instances')/target.instance)/'.env')
            if int(values.get('PORT', '0')) != target.port:
                raise ValueError('configured port mismatch')
            onebot_status(target.instance, values)
        except (OSError, RuntimeError, ValueError):
            websocket = False
    recent = _run(
        "journalctl",
        "-u",
        target.napcat_unit,
        "--since",
        "10 minutes ago",
        "--no-pager",
        check=False,
    )
    return Metrics(
        qq_fd_max=max((total for total, _ in qq_counts), default=0),
        maps_fd_max=max((maps for _, maps in qq_counts), default=0),
        xvfb_fd_max=max(xvfb_counts, default=0),
        maximum_clients="Maximum number of clients reached" in recent,
        websocket_established=websocket,
    )


def load_state(target: RuntimeTarget) -> State:
    try:
        return State(**json.loads(target.state_path.read_text(encoding="utf-8")))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return State()


def save_state(target: RuntimeTarget, state: State) -> None:
    target.state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.state_path.with_suffix(f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(asdict(state)), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(target.state_path)


def wait_for_recovery(target: RuntimeTarget, timeout_seconds: int = 90) -> Metrics:
    deadline = time.monotonic() + timeout_seconds
    latest = Metrics(0, 0, 0, False, False)
    while time.monotonic() < deadline:
        try:
            active = (
                _run("systemctl", "is-active", target.napcat_unit, check=False).strip()
                == "active"
            )
            bot_active = (
                _run("systemctl", "is-active", target.bot_unit, check=False).strip()
                == "active"
            )
            latest = collect_metrics(target)
            if active and bot_active and latest.websocket_established and latest.qq_fd_max < QQ_FD_LIMIT and latest.maps_fd_max < MAPS_FD_LIMIT and latest.xvfb_fd_max < XVFB_FD_LIMIT:
                return latest
        except (OSError, RuntimeError, ValueError):
            pass
        time.sleep(3)
    raise RuntimeError(f"NapCat post-check failed: {asdict(latest)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', action='version', version=OPS_VERSION)
    parser.add_argument("--instance", choices=("carrot", "kona"), default="carrot")
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument('--root', type=Path, default=Path('/opt/qq-bots'))
    args = parser.parse_args()
    target = target_for_instance(args.instance, args.root)
    if args.check_only:
        # A point-in-time inspection must not create a directory/lock, truncate
        # an existing lock, reset failures, or influence the next timer run.
        metrics = collect_metrics(target)
        decision = decide(metrics, load_state(target), int(time.time()), scheduled=args.scheduled)
        print(json.dumps({'check_only': True, 'ops_version': OPS_VERSION,
                          'metrics': asdict(metrics), 'decision': asdict(decision)}, ensure_ascii=True))
        return 0
    target.lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.lock_path.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            now_epoch = int(time.time())
            state = load_state(target)
            metrics = collect_metrics(target)
            decision = decide(metrics, state, now_epoch, scheduled=args.scheduled)
            print(json.dumps({"metrics": asdict(metrics), "decision": asdict(decision)}, ensure_ascii=True))
            if not decision.restart:
                save_state(target, decision.next_state)
                return 0
            subprocess.run(["systemctl", "restart", target.napcat_unit], check=True, timeout=60)
            next_state = replace(decision.next_state, last_restart_epoch=now_epoch, websocket_failures=0)
            save_state(target, next_state)
            recovered = wait_for_recovery(target)
            print(json.dumps({"post_restart": asdict(recovered)}, ensure_ascii=True))
            return 0
    except BlockingIOError:
        print("watchdog already running")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
