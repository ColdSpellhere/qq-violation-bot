#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
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
    previous = current.resolve(strict=True) if current.is_symlink() else None
    lock_path = Path(root) / "deploy.lock"
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open("r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _atomic_symlink(current, release)
        try:
            restart(instance)
            if not health(instance, sha):
                raise DeploymentError("instance health verification failed")
        except BaseException:
            if previous is not None:
                _atomic_symlink(current, previous)
            else:
                current.unlink(missing_ok=True)
            restart(instance)
            raise
    return sha


def prepare_release(repo: Path, root: Path, sha: str) -> Path:
    if SHA_RE.fullmatch(sha) is None:
        raise DeploymentError("sha must be 40 lowercase hexadecimal characters")
    repo = Path(repo)
    root = Path(root)
    if not repo.is_absolute() or repo.is_symlink() or not repo.is_dir():
        raise DeploymentError("repository must be an absolute non-symlink directory")
    subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
        check=True,
    )
    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    destination = releases / sha
    if destination.is_dir():
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
        os.replace(export, destination)
    return destination


def prune_unreferenced_releases(root: Path, *, keep: int = 5) -> None:
    releases = Path(root) / "releases"
    referenced = {
        current.resolve()
        for current in (Path(root) / "instances").glob("*/current")
        if current.is_symlink()
    }
    candidates = sorted(
        (path for path in releases.iterdir() if path.is_dir() and SHA_RE.fullmatch(path.name)),
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
    parser.add_argument("--repo", type=Path, default=Path("/opt/qq-bots/repository"))
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
