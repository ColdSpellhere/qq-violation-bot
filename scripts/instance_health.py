#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys
from collections.abc import Mapping
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from plugins.feature_control.state import FeatureController, FeatureState

try:
    from scripts.deploy_instance import DeploymentError, verify_release
except ImportError:
    from deploy_instance import DeploymentError, verify_release


INSTANCES = frozenset({"carrot", "kona"})
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
DEFAULT_REPOSITORY = Path("/opt/qq-bots/repository.git")
_HEALTH_FEATURE_DEFAULTS = FeatureState(
    business_enabled=False,
    chat_enabled=False,
    group_chat_enabled=False,
    private_chat_enabled=False,
    group_chat_allowed_group_ids=(),
    private_chat_allowed_user_ids=(),
)


def validate_runtime_state(
    instance: str,
    path: Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> None:
    path = Path(path)
    state = FeatureController._load_state(path, _HEALTH_FEATURE_DEFAULTS)
    if state is None:
        state = FeatureController._load_state(
            path.with_suffix(path.suffix + ".bak"),
            _HEALTH_FEATURE_DEFAULTS,
        )
    state_found = state is not None
    values = environment or {}
    if instance == "kona":
        if values.get("BOT_MODE", "").strip().lower() != "chat_only":
            raise ValueError("kona must remain a chat-only instance")
        truthy = {"1", "true", "yes", "on"}
        if any(
            values.get(name, "").strip().lower() in truthy
            for name in ("BUSINESS_ENABLED", "LLM_GATEWAY_BUSINESS_ENABLED")
        ):
            raise ValueError("kona chat-only environment cannot enable business")
        if state is not None and (
            state.business_enabled is not False
            or state.llm_gateway_business_enabled is not False
        ):
            raise ValueError("kona business capability must remain disabled")
    persisted_economy = state.economy_mode_enabled if state is not None else False
    requested_economy = persisted_economy if state_found else (
        values.get("ECONOMY_MODE_ENABLED", "").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    if requested_economy:
        valid_economy = (
            values.get(
                "GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"
            ).strip().rstrip("/")
            == "https://open.bigmodel.cn/api/paas/v4"
            and values.get("GLM_MODEL", "glm-4.7-flash").strip()
            == "glm-4.7-flash"
            and bool(values.get("GLM_API_KEY", "").strip())
        )
        if not valid_economy:
            raise ValueError("economy mode provider configuration is unavailable")


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
    validate_runtime_state(
        instance,
        instance_root / "data/runtime_features.json",
        environment=values,
    )


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
