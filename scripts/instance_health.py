#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from collections.abc import Mapping
from typing import Callable

sys.dont_write_bytecode = True

if __package__:
    from .deploy_instance import DeploymentError, verify_release
    from .ops_runtime import OPS_VERSION, EXPECTED_PORTS, cgroup_pids, exact_onebot_sockets, onebot_status, read_environment, tool_identity
else:
    from deploy_instance import DeploymentError, verify_release
    from ops_runtime import OPS_VERSION, EXPECTED_PORTS, cgroup_pids, exact_onebot_sockets, onebot_status, read_environment, tool_identity


INSTANCES = frozenset({"carrot", "kona"})
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
DEFAULT_REPOSITORY = Path("/opt/qq-bots/repository.git")
def _feature_types(release: Path):
    # Load only the pure state parser from the already source-verified release;
    # importing plugins.feature_control would register runtime matchers.
    path = release / 'plugins/feature_control/state.py'
    name = '_qqbot_health_feature_state'
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError('release feature state parser is unavailable')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    exec(compile(path.read_text(encoding='utf-8'), str(path), 'exec'), module.__dict__)
    return module.FeatureController, module.FeatureState


def validate_runtime_state(
    instance: str,
    path: Path,
    *,
    environment: Mapping[str, str] | None = None,
    release: Path | None = None,
) -> None:
    FeatureController, FeatureState = _feature_types(release or Path(__file__).resolve().parents[1])
    defaults = FeatureState(False, False, False, False, (), ())
    path = Path(path)
    state = FeatureController._load_state(path, defaults)
    if state is None:
        state = FeatureController._load_state(
            path.with_suffix(path.suffix + ".bak"),
            defaults,
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
    # Older release parsers predate this switch; their runtime has no economy
    # capability, so inspecting them during rollback must use the old default.
    persisted_economy = getattr(state, 'economy_mode_enabled', False)
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
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=8).stdout


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


def verify_running_release(pid: int, release: Path, *, proc_root: Path = Path('/proc')) -> None:
    process = proc_root/str(pid)
    if (process/'cwd').resolve(strict=True) != release.resolve(strict=True):
        raise RuntimeError('running bot working directory does not match requested release')
    arguments = (process/'cmdline').read_bytes().split(b'\0')
    if not arguments or b'bot.py' not in arguments or not Path(os.fsdecode(arguments[0])).name.startswith('python'):
        raise RuntimeError('systemd main process is not the expected bot entrypoint')


def verify(
    instance: str,
    sha: str,
    root: Path,
    *,
    repo: Path = DEFAULT_REPOSITORY,
) -> dict[str, object]:
    if instance not in INSTANCES or SHA_RE.fullmatch(sha) is None:
        raise ValueError("invalid instance or sha")
    root = Path(root)
    instance_root = root / "instances" / instance
    current = instance_root / "current"
    expected_release = root / "releases" / sha
    if expected_release.is_symlink() or not expected_release.is_dir():
        raise RuntimeError('requested release path is missing or unsafe')
    if not current.is_symlink() or current.resolve() != expected_release:
        raise RuntimeError("instance release pointer does not match requested sha")
    try:
        verify_release(Path(repo), expected_release, sha, verify_environment=True)
    except DeploymentError as exc:
        raise RuntimeError(str(exc)) from exc
    if _run("systemctl", "is-active", f"qqbot@{instance}.service").strip() != "active":
        raise RuntimeError("service is not active")
    values = read_environment(instance_root / '.env')
    port = int(values["PORT"])
    if port != EXPECTED_PORTS[instance]:
        raise RuntimeError('instance reverse WebSocket port is incorrect')
    bot_pids = cgroup_pids(f'qqbot@{instance}.service', _run)
    napcat_pids = cgroup_pids(f'napcat@{instance}.service', _run)
    pid_text = _run('systemctl', 'show', f'qqbot@{instance}.service', '-p', 'MainPID', '--value').strip()
    if not pid_text.isdigit() or int(pid_text) not in bot_pids:
        raise RuntimeError('systemd main process is outside expected bot cgroup')
    verify_running_release(int(pid_text), expected_release)
    if not exact_onebot_sockets(_run('ss', '-Htanp'), port, bot_pids, napcat_pids):
        raise RuntimeError('exact instance-owned OneBot loopback socket pair is missing')
    status = onebot_status(instance, values)
    validate_runtime_state(
        instance,
        instance_root / "data/runtime_features.json",
        environment=values,
        release=expected_release,
    )
    # Historical errors in a live invocation are diagnostic, not a permanent
    # health latch. Current identity/API/socket/config probes determine health.
    return {'instance': instance, 'release_sha': sha, **tool_identity(Path(__file__)), **status}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', action='version', version=OPS_VERSION)
    parser.add_argument("--instance", choices=sorted(INSTANCES), required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--root", type=Path, default=Path("/opt/qq-bots"))
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPOSITORY)
    parser.add_argument('--expected-ops-version')
    args = parser.parse_args()
    if args.expected_ops_version and args.expected_ops_version != OPS_VERSION:
        parser.error('operational tool version mismatch')
    try:
        print(json.dumps(verify(args.instance, args.sha, args.root, repo=args.repo), sort_keys=True))
    except Exception as exc:
        # Deliberately omit subprocess stderr and upstream response text.
        print(json.dumps({'healthy': False, 'error': str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__,
                          'ops_version': OPS_VERSION}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
