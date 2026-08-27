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


INSTANCES = frozenset({"carrot", "kona"})
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
MANIFEST_NAME = ".release-manifest.json"
MANIFEST_KEYS = frozenset({"format_version", "commit", "tree", "source_sha256"})
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


def verify_release(repo: Path, release: Path, sha: str) -> dict[str, object]:
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
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_KEYS:
        raise DeploymentError("release manifest has an invalid schema")
    if manifest.get("format_version") != 1:
        raise DeploymentError("release manifest format version is unsupported")
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
    return manifest


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
) -> str:
    _, instance_root, release = _validate(instance, sha, Path(root))
    instance_root.mkdir(parents=True, exist_ok=True)
    current = instance_root / "current"
    previous_link = instance_root / "previous"
    lock_path = Path(root) / "deploy.lock"
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open("r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        previous_release = current.resolve(strict=True) if current.is_symlink() else None
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
        except BaseException:
            if not same_release and previous_release is not None:
                _atomic_symlink(current, previous_release)
            elif not same_release:
                current.unlink(missing_ok=True)
            restart(instance)
            raise
    return sha


def prepare_release(repo: Path, root: Path, sha: str) -> Path:
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
            verify_release(repo, destination, sha)
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
            root = export.resolve()
            for member in bundle.getmembers():
                if member.issym() or member.islnk():
                    raise DeploymentError("release archive links are not allowed")
                if not member.isdir() and not member.isfile():
                    raise DeploymentError("release archive contains a special file")
                destination_path = (export / member.name).resolve()
                if not destination_path.is_relative_to(root):
                    raise DeploymentError("release archive contains an unsafe path")
            bundle.extractall(export)
        subprocess.run([sys.executable, "-m", "venv", str(export / ".venv")], check=True)
        subprocess.run(
            [str(export / ".venv/bin/pip"), "install", "-r", str(export / "requirements.txt")],
            check=True,
        )
        subprocess.run(
            [str(export / ".venv/bin/python"), "-m", "compileall", "-q", str(export)],
            check=True,
        )
        _remove_source_bytecode(export)
        source_sha256 = release_source_sha256(repo, export, sha)
        (export / MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "commit": commit,
                    "tree": tree,
                    "source_sha256": source_sha256,
                },
                ensure_ascii=True,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        verify_release(repo, export, sha)
        os.replace(export, destination)
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", required=True, choices=sorted(INSTANCES))
    parser.add_argument("--sha", required=True)
    parser.add_argument("--root", type=Path, default=Path("/opt/qq-bots"))
    parser.add_argument("--repo", type=Path, default=Path("/opt/qq-bots/repository.git"))
    args = parser.parse_args()
    prepare_release(args.repo, args.root, args.sha)

    def restart(instance: str) -> None:
        subprocess.run(["systemctl", "restart", f"qqbot@{instance}.service"], check=True)

    def health(instance: str, sha: str) -> bool:
        command = [
            sys.executable,
            str(Path(__file__).with_name("instance_health.py")),
            "--instance",
            instance,
            "--sha",
            sha,
            "--root",
            str(args.root),
            "--repo",
            str(args.repo),
        ]
        return wait_for_health(
            lambda: subprocess.run(
                command,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )

    deploy_existing_release(
        args.instance, args.sha, args.root, restart=restart, health=health
    )
    prune_unreferenced_releases(args.root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
