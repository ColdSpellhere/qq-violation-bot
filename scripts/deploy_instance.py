#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Callable

sys.dont_write_bytecode = True
if __package__:
    from .ops_runtime import OPS_VERSION, tool_identity
else:
    from ops_runtime import OPS_VERSION, tool_identity


INSTANCES = frozenset({"carrot", "kona"})
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
MANIFEST_NAME = ".release-manifest.json"
MANIFEST_KEYS = frozenset({"format_version", "commit", "tree", "source_sha256"})
BUILD_KEYS = frozenset({'python_version', 'python_implementation', 'python_executable_sha256', 'pip_freeze_sha256',
    'requirements_file', 'requirements_sha256', 'ops_version', 'tool_sha256', 'runtime_sha256'})
REGULAR_GIT_MODES = frozenset({"100644", "100755"})


class DeploymentError(RuntimeError):
    pass


def wait_for_health(
    probe: Callable[[], bool],
    *,
    timeout_seconds: float = 60,
    interval_seconds: float = 2,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    deadline = monotonic() + timeout_seconds
    while True:
        if probe():
            return True
        if monotonic() >= deadline:
            return False
        sleep(interval_seconds)


def _validate(instance: str, sha: str, root: Path) -> tuple[Path, Path, Path]:
    if instance not in INSTANCES:
        raise DeploymentError("instance must be carrot or kona")
    if SHA_RE.fullmatch(sha) is None:
        raise DeploymentError("sha must be 40 lowercase hexadecimal characters")
    root = Path(root)
    if not root.is_absolute() or root.is_symlink():
        raise DeploymentError("deployment root must be an absolute non-symlink path")
    releases = root / "releases"
    instance_root = root / "instances" / instance
    release = releases / sha
    if not release.is_dir() or release.is_symlink():
        raise DeploymentError("requested release does not exist")
    return releases, instance_root, release


def _atomic_symlink(link: Path, target: Path) -> None:
    temporary = link.with_name(f".{link.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


def _validate_repository(repo: Path) -> Path:
    repo = Path(repo)
    if not repo.is_absolute() or repo.is_symlink() or not repo.is_dir():
        raise DeploymentError("repository must be an absolute non-symlink directory")
    return repo


def _git(
    repo: Path,
    *arguments: str,
    text: bool = False,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            capture_output=True,
            text=text,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        raise DeploymentError("repository does not contain the requested commit") from exc


def _commit_and_tree(repo: Path, sha: str) -> tuple[str, str]:
    commit = _git(
        repo, "rev-parse", "--verify", f"{sha}^{{commit}}", text=True
    ).stdout.strip()
    if commit != sha:
        raise DeploymentError("requested sha must identify the commit object itself")
    tree = _git(
        repo, "rev-parse", "--verify", f"{sha}^{{tree}}", text=True
    ).stdout.strip()
    if SHA_RE.fullmatch(tree) is None:
        raise DeploymentError("repository returned an invalid tree object id")
    return commit, tree


def _tracked_entries(repo: Path, sha: str) -> list[tuple[str, bytes, str, str]]:
    output = _git(repo, "ls-tree", "-r", "-z", "--full-tree", sha).stdout
    entries: list[tuple[str, bytes, str, str]] = []
    for record in output.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if separator != b"\t" or len(fields) != 3:
            raise DeploymentError("repository returned an invalid tracked source entry")
        mode_bytes, object_type, object_id_bytes = fields
        mode = mode_bytes.decode("ascii", "strict")
        object_id = object_id_bytes.decode("ascii", "strict")
        if object_type != b"blob" or mode not in REGULAR_GIT_MODES:
            raise DeploymentError("release source may contain only regular tracked files")
        path_text = os.fsdecode(raw_path)
        path = PurePosixPath(path_text)
        if (
            not raw_path
            or path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != path_text
        ):
            raise DeploymentError("repository contains an unsafe tracked source path")
        if (
            path.as_posix() == MANIFEST_NAME
            or (path.parts and path.parts[0] == ".venv")
            or "__pycache__" in path.parts
            or path.suffix == ".pyc"
        ):
            raise DeploymentError("repository tracks a reserved release artifact path")
        entries.append((path.as_posix(), raw_path, mode, object_id))
    entries.sort(key=lambda item: item[1])
    return entries


def _expected_directories(paths: set[str]) -> set[str]:
    directories: set[str] = set()
    for path_text in paths:
        for parent in PurePosixPath(path_text).parents:
            if parent != PurePosixPath("."):
                directories.add(parent.as_posix())
    return directories


def _validate_release_layout(release: Path, tracked_paths: set[str]) -> None:
    release = Path(release)
    if not release.is_absolute() or release.is_symlink() or not release.is_dir():
        raise DeploymentError("release must be an absolute non-symlink directory")
    expected_directories = _expected_directories(tracked_paths)
    for directory, names, files in os.walk(release, topdown=True, followlinks=False):
        base = Path(directory)
        for name in list(names):
            candidate = base / name
            relative = candidate.relative_to(release).as_posix()
            if candidate.is_symlink():
                raise DeploymentError(f"unexpected release file: {relative}")
            if relative == ".venv":
                names.remove(name)
                continue
            if relative not in expected_directories:
                raise DeploymentError(f"unexpected release file: {relative}")
        for name in files:
            candidate = base / name
            relative = candidate.relative_to(release).as_posix()
            if candidate.is_symlink() or not candidate.is_file():
                raise DeploymentError(f"unexpected release file: {relative}")
            if relative not in tracked_paths and relative != MANIFEST_NAME:
                raise DeploymentError(f"unexpected release file: {relative}")


def release_source_sha256(repo: Path, release: Path, sha: str) -> str:
    if SHA_RE.fullmatch(sha) is None:
        raise DeploymentError("sha must be 40 lowercase hexadecimal characters")
    repo = _validate_repository(repo)
    _commit_and_tree(repo, sha)
    entries = _tracked_entries(repo, sha)
    tracked_paths = {entry[0] for entry in entries}
    _validate_release_layout(Path(release), tracked_paths)
    digest = hashlib.sha256(b"qqbot-release-source-v1\0")
    for path_text, raw_path, mode, object_id in entries:
        candidate = Path(release).joinpath(*PurePosixPath(path_text).parts)
        if candidate.is_symlink() or not candidate.is_file():
            raise DeploymentError(f"tracked source is missing or unsafe: {path_text}")
        try:
            content = candidate.read_bytes()
            executable = bool(candidate.stat().st_mode & 0o111)
        except OSError as exc:
            raise DeploymentError(f"tracked source cannot be read: {path_text}") from exc
        actual_mode = "100755" if executable else "100644"
        if actual_mode != mode:
            raise DeploymentError(f"tracked source mode does not match commit: {path_text}")
        blob_header = f"blob {len(content)}\0".encode("ascii")
        actual_object_id = hashlib.sha1(blob_header + content).hexdigest()
        if actual_object_id != object_id:
            raise DeploymentError(f"tracked source does not match commit: {path_text}")
        for part in (mode.encode("ascii"), raw_path, content):
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
    return digest.hexdigest()


def verify_release(repo: Path, release: Path, sha: str, *, verify_environment: bool = False) -> dict[str, object]:
    if SHA_RE.fullmatch(sha) is None:
        raise DeploymentError("sha must be 40 lowercase hexadecimal characters")
    repo = _validate_repository(repo)
    commit, tree = _commit_and_tree(repo, sha)
    release = Path(release)
    manifest_path = release / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise DeploymentError("release manifest is missing or unsafe")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentError("release manifest is not valid JSON") from exc
    if not isinstance(manifest, dict) or set(manifest) not in (MANIFEST_KEYS, MANIFEST_KEYS | {'build'}):
        raise DeploymentError("release manifest has an invalid schema")
    if manifest.get("format_version") not in (1, 2):
        raise DeploymentError("release manifest format version is unsupported")
    if (manifest['format_version'] == 2) != ('build' in manifest):
        raise DeploymentError('release manifest build metadata is missing or inconsistent')
    if manifest.get("commit") != commit:
        raise DeploymentError("release manifest commit does not match requested sha")
    if manifest.get("tree") != tree:
        raise DeploymentError("release manifest tree does not match repository commit")
    source_sha256 = manifest.get("source_sha256")
    if not isinstance(source_sha256, str) or SHA256_RE.fullmatch(source_sha256) is None:
        raise DeploymentError("release manifest source hash is invalid")
    actual_source_sha256 = release_source_sha256(repo, release, sha)
    if source_sha256 != actual_source_sha256:
        raise DeploymentError("release manifest source hash does not match tracked source")
    if manifest['format_version'] == 2:
        build = manifest['build']
        if not isinstance(build, dict) or set(build) != BUILD_KEYS:
            raise DeploymentError('release build metadata schema is invalid')
        for key in ('python_executable_sha256', 'pip_freeze_sha256', 'requirements_sha256', 'tool_sha256', 'runtime_sha256'):
            if not isinstance(build[key], str) or SHA256_RE.fullmatch(build[key]) is None:
                raise DeploymentError('release build metadata digest is invalid')
        if build['requirements_file'] not in {'requirements.txt', 'requirements.lock'}:
            raise DeploymentError('release requirements source is invalid')
        if hashlib.sha256((release/build['requirements_file']).read_bytes()).hexdigest() != build['requirements_sha256']:
            raise DeploymentError('release requirements digest does not match build metadata')
        if verify_environment:
            actual = inspect_environment(release)
            for key in ('python_version', 'python_implementation', 'python_executable_sha256', 'pip_freeze_sha256'):
                if actual[key] != build[key]:
                    raise DeploymentError(f'release environment drift: {key}')
    return manifest


def validate_venv_entrypoints(release: Path) -> None:
    venv = release / '.venv'
    binary = venv/'bin'
    python = binary/'python'
    if not python.is_file():
        raise DeploymentError('release interpreter is missing')
    activate = binary/'activate'
    if not activate.is_file() or str(venv) not in activate.read_text(encoding='utf-8'):
        raise DeploymentError('venv activation does not reference its final release path')
    for entry in binary.iterdir():
        if not entry.is_file() or entry.is_symlink():
            continue
        with entry.open('rb') as stream:
            prefix = stream.read(4096)
        if not prefix.startswith(b'#!'):
            continue
        text = prefix.decode('utf-8', errors='replace')
        interpreter = text.splitlines()[0][2:].strip().split(' ', 1)[0]
        if '.venv/' in interpreter or Path(interpreter).name.startswith('python'):
            path = Path(interpreter)
            if path.parent != binary or not path.is_file():
                raise DeploymentError(f'venv console entrypoint has a stale interpreter: {entry.name}')
        # pip uses a shell trampoline if an interpreter path is long/spaced.
        for matched in re.findall(r'''["']([^"'\n]*\.venv/bin/python[^"'\n]*)["']''', text):
            if Path(matched).parent != binary or not Path(matched).is_file():
                raise DeploymentError(f'venv entrypoint trampoline is stale: {entry.name}')


def _environment_run(release: Path, *arguments: str) -> str:
    result = subprocess.run([str(release/'.venv/bin/python'), '-B', *arguments], check=True,
        text=True, capture_output=True, timeout=120,
        env={**os.environ, 'PIP_DISABLE_PIP_VERSION_CHECK': '1', 'PYTHONDONTWRITEBYTECODE': '1'})
    return result.stdout


def inspect_environment(release: Path) -> dict[str, str]:
    validate_venv_entrypoints(release)
    raw = _environment_run(release, '-c',
        'import json,sys,platform; print(json.dumps({"prefix":sys.prefix,"python_version":platform.python_version(),"python_implementation":sys.implementation.name}))')
    data = json.loads(raw)
    if Path(data.pop('prefix')) != release/'.venv':
        raise DeploymentError('interpreter prefix does not match final release')
    _environment_run(release, '-m', 'pip', 'check')
    freeze = _environment_run(release, '-m', 'pip', 'freeze', '--all')
    canonical = '\n'.join(sorted(line.strip() for line in freeze.splitlines() if line.strip()))+'\n'
    data['pip_freeze_sha256'] = hashlib.sha256(canonical.encode()).hexdigest()
    data['python_executable_sha256'] = hashlib.sha256((release/'.venv/bin/python').read_bytes()).hexdigest()
    return data


def build_environment(release: Path) -> dict[str, str]:
    requirements = release/'requirements.lock'
    if not requirements.is_file():
        requirements = release/'requirements.txt'
    else:
        for line in requirements.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and re.fullmatch(r'[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[A-Za-z0-9_.+!-]+(?:\s*;[^\n]+)?', line) is None:
                raise DeploymentError('requirements.lock must contain exact package==version pins')
    subprocess.run([sys.executable, '-m', 'venv', str(release/'.venv')], check=True, timeout=120)
    _environment_run(release, '-m', 'pip', 'install', '-r', str(requirements))
    _environment_run(release, '-m', 'compileall', '-q', '-x', r'[/\\]\.venv[/\\]', str(release))
    _remove_source_bytecode(release)
    return {**inspect_environment(release), **tool_identity(Path(__file__)),
            'requirements_file': requirements.name,
            'requirements_sha256': hashlib.sha256(requirements.read_bytes()).hexdigest()}


def _remove_source_bytecode(release: Path) -> None:
    for directory, names, _files in os.walk(release, topdown=True, followlinks=False):
        if Path(directory) == release and ".venv" in names:
            names.remove(".venv")
        for name in list(names):
            if name == "__pycache__":
                shutil.rmtree(Path(directory) / name)
                names.remove(name)


def deploy_existing_release(
    instance: str,
    sha: str,
    root: Path,
    *,
    restart: Callable[[str], None],
    health: Callable[[str, str], bool],
    stop: Callable[[str], None] | None = None,
) -> str:
    _, instance_root, release = _validate(instance, sha, Path(root))
    instance_root.mkdir(parents=True, exist_ok=True)
    current = instance_root / "current"
    previous_link = instance_root / "previous"
    lock_path = Path(root) / "deploy.lock"
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open("r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if current.exists() and not current.is_symlink():
            raise DeploymentError('current must be a release symlink')
        previous_release = current.resolve(strict=True) if current.is_symlink() else None
        if previous_release is not None and (previous_release.parent != release.parent or SHA_RE.fullmatch(previous_release.name) is None):
            raise DeploymentError('current points outside the validated release namespace')
        same_release = previous_release == release.resolve(strict=True)
        if not same_release:
            _atomic_symlink(current, release)
        try:
            restart(instance)
            if not health(instance, sha):
                raise DeploymentError("instance health verification failed")
            if not same_release and previous_release is not None:
                _atomic_symlink(previous_link, previous_release)
            elif not same_release:
                previous_link.unlink(missing_ok=True)
        except BaseException as original:
            rollback = 'not_attempted'
            rollback_error = None
            if not same_release and previous_release is not None:
                _atomic_symlink(current, previous_release)
                try:
                    restart(instance)
                    if not health(instance, previous_release.name):
                        raise DeploymentError('old release health verification failed')
                    rollback = 'healthy'
                except BaseException as exc:
                    rollback, rollback_error = 'failed', type(exc).__name__
            elif not same_release:
                current.unlink(missing_ok=True)
                if stop is not None:
                    try:
                        stop(instance)
                        rollback = 'no_previous_release_stopped'
                    except BaseException as exc:
                        rollback, rollback_error = 'stop_failed', type(exc).__name__
                else:
                    rollback = 'no_previous_release_stop_unavailable'
            else:
                rollback = 'same_release_no_rollback'
            report = {'instance': instance, 'requested_sha': sha,
                      'previous_sha': previous_release.name if previous_release else None,
                      'failure_type': type(original).__name__, 'rollback': rollback,
                      'rollback_failure_type': rollback_error, 'timestamp': int(time.time()),
                      **tool_identity(Path(__file__))}
            report_path = instance_root/'.deployment-result.json'
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=instance_root,
                                             prefix='.deployment-result.', delete=False) as output:
                json.dump(report, output, sort_keys=True)
                output.write('\n')
                temporary = Path(output.name)
            os.replace(temporary, report_path)
            raise DeploymentError(f'deployment health/start failed ({type(original).__name__}); rollback {rollback}') from original
    return sha


def prepare_release(repo: Path, root: Path, sha: str) -> Path:
    root = Path(root)
    if not root.is_absolute() or root.is_symlink():
        raise DeploymentError('deployment root must be an absolute non-symlink path')
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root/'deploy.lock'
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open('r+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return _prepare_release_locked(repo, root, sha)


def _prepare_release_locked(repo: Path, root: Path, sha: str) -> Path:
    if SHA_RE.fullmatch(sha) is None:
        raise DeploymentError("sha must be 40 lowercase hexadecimal characters")
    repo = _validate_repository(repo)
    root = Path(root)
    if not root.is_absolute() or root.is_symlink():
        raise DeploymentError("deployment root must be an absolute non-symlink path")
    commit, tree = _commit_and_tree(repo, sha)
    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    destination = releases / sha
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_dir():
            raise DeploymentError("existing release path is not a safe directory")
        try:
            manifest = verify_release(repo, destination, sha, verify_environment=True)
            if manifest['format_version'] != 2:
                raise DeploymentError('legacy build has no reproducible environment manifest; use a new commit')
        except DeploymentError as exc:
            raise DeploymentError(
                f"existing release cannot be safely reused: {exc}"
            ) from exc
        return destination
    with tempfile.TemporaryDirectory(dir=releases, prefix=f".{sha}.") as temporary:
        staging = Path(temporary)
        archive = staging / "release.tar"
        subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", "-o", str(archive), sha],
            check=True,
        )
        export = staging / "export"
        export.mkdir()
        with tarfile.open(archive) as bundle:
            export_root = export.resolve()
            for member in bundle.getmembers():
                if member.issym() or member.islnk():
                    raise DeploymentError("release archive links are not allowed")
                if not member.isdir() and not member.isfile():
                    raise DeploymentError("release archive contains a special file")
                destination_path = (export / member.name).resolve()
                if not destination_path.is_relative_to(export_root):
                    raise DeploymentError("release archive contains an unsafe path")
            bundle.extractall(export)
        source_sha256 = release_source_sha256(repo, export, sha)
        # Console entrypoints and activate embed absolute paths. Build only
        # after source reaches its final path, with no current pointer switch.
        os.replace(export, destination)
        try:
            build = build_environment(destination)
            (destination / MANIFEST_NAME).write_text(
                json.dumps(
                    {'format_version': 2, 'commit': commit, 'tree': tree,
                     'source_sha256': source_sha256, 'build': build},
                    ensure_ascii=True, sort_keys=True,
                ) + '\n', encoding='utf-8',
            )
            verify_release(repo, destination, sha)
        except BaseException:
            shutil.rmtree(destination)
            raise
    return destination


def prune_unreferenced_releases(root: Path, *, keep: int = 5) -> None:
    root = Path(root)
    lock_path = root / "deploy.lock"
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open("r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        releases = root / "releases"
        referenced = set()
        for pointer_name in ("current", "previous"):
            referenced.update(
                pointer.resolve()
                for pointer in (root / "instances").glob(f"*/{pointer_name}")
                if pointer.is_symlink()
            )
        candidates = sorted(
            (
                path
                for path in releases.iterdir()
                if path.is_dir()
                and not path.is_symlink()
                and SHA_RE.fullmatch(path.name)
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in candidates[keep:]:
            if path.resolve() not in referenced:
                shutil.rmtree(path)


def wait_for_command_health(command: list[str], *, timeout_seconds: float = 60,
                            monotonic=time.monotonic, sleep=time.sleep) -> bool:
    deadline = monotonic() + timeout_seconds
    last_error = 'probe_not_run'
    while (remaining := deadline - monotonic()) > 0:
        try:
            result = subprocess.run(command, check=False, text=True, capture_output=True,
                                    timeout=min(20, remaining))
            if result.returncode == 0:
                print(result.stdout.strip())
                return True
            try:
                last_error = str(json.loads(result.stderr).get('error', 'probe_failed'))[:240]
            except (ValueError, AttributeError):
                last_error = 'probe_failed_without_structured_result'
        except subprocess.TimeoutExpired:
            last_error = 'probe_timeout'
        remaining = deadline - monotonic()
        if remaining > 0:
            sleep(min(2, remaining))
    print(json.dumps({'healthy': False, 'last_probe_error': last_error}), file=sys.stderr)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', action='version', version=OPS_VERSION)
    parser.add_argument("--instance", required=True, choices=sorted(INSTANCES))
    parser.add_argument("--sha", required=True)
    parser.add_argument("--root", type=Path, default=Path("/opt/qq-bots"))
    parser.add_argument("--repo", type=Path, default=Path("/opt/qq-bots/repository.git"))
    args = parser.parse_args()
    prepare_release(args.repo, args.root, args.sha)

    def restart(instance: str) -> None:
        subprocess.run(["systemctl", "restart", f"qqbot@{instance}.service"], check=True, timeout=60)

    def stop(instance: str) -> None:
        subprocess.run(['systemctl', 'stop', f'qqbot@{instance}.service'], check=True, timeout=30)

    def health(instance: str, sha: str) -> bool:
        command = [
            sys.executable,
            '-B',
            str(Path(__file__).with_name("instance_health.py")),
            "--instance",
            instance,
            "--sha",
            sha,
            "--root",
            str(args.root),
            "--repo",
            str(args.repo),
            '--expected-ops-version', OPS_VERSION,
        ]
        return wait_for_command_health(command)

    deploy_existing_release(
        args.instance, args.sha, args.root, restart=restart, health=health, stop=stop
    )
    prune_unreferenced_releases(args.root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
