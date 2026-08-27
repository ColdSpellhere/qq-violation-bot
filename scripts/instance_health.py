#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
from typing import Callable

try:
    from scripts.deploy_instance import DeploymentError, verify_release
except ImportError:
    from deploy_instance import DeploymentError, verify_release


INSTANCES = frozenset({"carrot", "kona"})
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
DEFAULT_REPOSITORY = Path("/opt/qq-bots/repository.git")


def validate_runtime_state(instance: str, path: Path) -> None:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    if not isinstance(raw, dict):
        raise ValueError("runtime feature state must be an object")
    if instance == "kona" and (
        raw.get("business_enabled") is not False
        or raw.get("llm_gateway_business_enabled") is not False
    ):
        raise ValueError("kona business capability must remain disabled")


def _run(*args: str) -> str:
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout


def current_invocation_logs(
    unit: str,
    *,
    run: Callable[..., str] = _run,
) -> str:
    invocation_id = run(
        "systemctl", "show", unit, "-p", "InvocationID", "--value"
    ).strip()
    if re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None:
        raise RuntimeError("service has no current systemd invocation")
    return run("journalctl", f"_SYSTEMD_INVOCATION_ID={invocation_id}", "--no-pager")


def verify(
    instance: str,
    sha: str,
    root: Path,
    *,
    repo: Path = DEFAULT_REPOSITORY,
) -> None:
    if instance not in INSTANCES or SHA_RE.fullmatch(sha) is None:
        raise ValueError("invalid instance or sha")
    root = Path(root)
    instance_root = root / "instances" / instance
    current = instance_root / "current"
    expected_release = (root / "releases" / sha).resolve()
    if not current.is_symlink() or current.resolve() != expected_release:
        raise RuntimeError("instance release pointer does not match requested sha")
    try:
        verify_release(Path(repo), expected_release, sha)
    except DeploymentError as exc:
        raise RuntimeError(str(exc)) from exc
    if _run("systemctl", "is-active", f"qqbot@{instance}.service").strip() != "active":
        raise RuntimeError("service is not active")
    values: dict[str, str] = {}
    for line in (instance_root / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"\'')
    port = int(values["PORT"])
    sockets = _run("ss", "-Htan")
    if f"127.0.0.1:{port}" not in sockets:
        raise RuntimeError("loopback port is not listening")
    if not any(
        "ESTAB" in line and f"127.0.0.1:{port}" in line
        for line in sockets.splitlines()
    ):
        raise RuntimeError("OneBot loopback connection is not established")
    logs = current_invocation_logs(f"qqbot@{instance}.service").lower()
    if "traceback (most recent call last)" in logs or "critical" in logs:
        raise RuntimeError("recent service logs contain a startup failure")
    validate_runtime_state(instance, instance_root / "data/runtime_features.json")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", choices=sorted(INSTANCES), required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--root", type=Path, default=Path("/opt/qq-bots"))
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPOSITORY)
    args = parser.parse_args()
    verify(args.instance, args.sha, args.root, repo=args.repo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
