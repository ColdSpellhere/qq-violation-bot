#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess


INSTANCES = frozenset({"carrot", "kona"})
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")


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


def verify(instance: str, sha: str, root: Path) -> None:
    if instance not in INSTANCES or SHA_RE.fullmatch(sha) is None:
        raise ValueError("invalid instance or sha")
    instance_root = Path(root) / "instances" / instance
    current = instance_root / "current"
    if not current.is_symlink() or current.resolve().name != sha:
        raise RuntimeError("instance release pointer does not match requested sha")
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
    logs = _run(
        "journalctl",
        "-u",
        f"qqbot@{instance}.service",
        "-n",
        "100",
        "--no-pager",
    ).lower()
    if "traceback (most recent call last)" in logs or "critical" in logs:
        raise RuntimeError("recent service logs contain a startup failure")
    validate_runtime_state(instance, instance_root / "data/runtime_features.json")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", choices=sorted(INSTANCES), required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--root", type=Path, default=Path("/opt/qq-bots"))
    args = parser.parse_args()
    verify(args.instance, args.sha, args.root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
