#!/usr/bin/env python3
"""Independent, verifiable instance backups. Never import business plugins."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tarfile
import time

FORMAT = "qqbot-instance-backup-v1"
_SNAPSHOT = re.compile(r"\d{8}T\d{12}Z-(state|full)\Z")
_STATE_SUFFIXES = {".json", ".jsonl", ".yaml", ".yml", ".toml", ".txt", ".md", ".env"}


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return _stream_hash(stream)


def _stream_hash(stream) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def _safe(path: Path) -> Path:
    if not path.is_absolute() or any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("backup source or destination uses a symbolic link")
    return path


def _env(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            result[key.strip()] = value
    return result


def _sqlite_copy(source: Path, target: Path, deadline: float) -> dict:
    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError("database backup deadline exceeded")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)) as src:
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst, pages=256, progress=progress, sleep=0.05)
            if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("backup database integrity failure")
            counts = {name: dst.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                for (name,) in dst.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    target.chmod(0o600)
    stat = source.stat()
    return {"source": str(source), "device": stat.st_dev, "inode": stat.st_ino,
        "file": str(target.relative_to(target.parent.parent)), "bytes": target.stat().st_size,
        "sha256": _hash(target), "table_counts": counts, "integrity": "ok"}


def create_backup(root: Path, instance: str, *, mode: str = "state", extra_dirs: tuple[Path, ...] = ()) -> Path:
    root = _safe(Path(root))
    if instance not in {"carrot", "kona"} or mode not in {"state", "full"}:
        raise ValueError("invalid instance or backup mode")
    source = _safe(root / "instances" / instance)
    values = _env(_safe(source / ".env"))
    managed = _safe(root / "backups" / "managed-v1" / instance)
    managed.mkdir(parents=True, exist_ok=True, mode=0o700)
    managed.chmod(0o700)
    lock_path = managed / ".backup.lock"
    with lock_path.open("a+") as lock:
        lock_path.chmod(0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + mode
        pending = managed / (".pending-" + stamp)
        pending.mkdir(mode=0o700)
        (pending / "databases").mkdir(mode=0o700)
        files: dict[str, Path] = {}
        roots = [source / ".env", source / "character.md", source / "data"]
        if mode == "full":
            roots.extend([source / "evidence", source / "exports"])
        for index, directory in enumerate([*roots, *extra_dirs]):
            if not directory.exists():
                if directory in extra_dirs:
                    raise ValueError("explicit extra backup directory is missing")
                continue
            _safe(directory)
            candidates = [directory] if directory.is_file() else sorted(directory.rglob("*"))
            for item in candidates:
                _safe(item)
                if not item.is_file() or item.name.endswith(("-wal", "-shm", "-journal", ".lock")):
                    continue
                name = str(item.relative_to(source)) if item.is_relative_to(source) else f"extra-{index}/" + str(item.relative_to(directory))
                files[name] = item
        databases = set()
        for item in files.values():
            with item.open("rb") as stream:
                if stream.read(16) == b"SQLite format 3\x00":
                    databases.add(item)
        url = values.get("DATABASE_URL", "")
        if url.startswith("sqlite:///"):
            configured = Path(url[len("sqlite:///"):])
            if not configured.is_absolute():
                configured = (source / "current").resolve() / configured
            if configured.exists():
                databases.add(_safe(configured))
            elif values.get("BOT_MODE", "full") != "chat_only":
                raise ValueError("configured business database is missing")
        manifest = {"format": FORMAT, "instance": instance, "mode": mode,
            "created_at": datetime.now(timezone.utc).isoformat(), "source_root": str(source),
            "consistency": "per-database online snapshot; files captured during the stated interval",
            "current": str((source / "current").resolve()), "databases": [], "files": {},
            "media_included": mode == "full"}
        for index, database in enumerate(sorted(databases)):
            manifest["databases"].append(_sqlite_copy(database, pending / "databases" / f"{index}-{database.name}.sqlite3", time.monotonic() + 120))
        archive = pending / "files.tar.gz"
        with tarfile.open(archive, "w:gz", compresslevel=1) as tar:
            for name, path in files.items():
                if path in databases:
                    continue
                if mode == "state" and path.name not in {".env", "character.md"} and path.suffix.lower() not in _STATE_SUFFIXES:
                    continue
                manifest["files"][name] = {"bytes": path.stat().st_size, "sha256": _hash(path)}
                tar.add(path, arcname=name, recursive=False)
        archive.chmod(0o600)
        manifest["archive_sha256"] = _hash(archive)
        manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
        output = pending / "manifest.json"
        output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        output.chmod(0o600)
        verify_backup(pending)
        final = managed / stamp
        os.replace(pending, final)
        return final


def verify_backup(snapshot: Path) -> dict:
    snapshot = _safe(Path(snapshot))
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT:
        raise ValueError("unsupported backup manifest")
    archive = snapshot / "files.tar.gz"
    if _hash(archive) != manifest["archive_sha256"]:
        raise ValueError("backup archive checksum mismatch")
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if set(item.name for item in members) != set(manifest["files"]):
            raise ValueError("backup archive file list mismatch")
        for member in members:
            if not member.isfile():
                raise ValueError("backup archive contains non-regular data")
            with tar.extractfile(member) as stream:
                if _stream_hash(stream) != manifest["files"][member.name]["sha256"]:
                    raise ValueError("backup file checksum mismatch")
    for item in manifest["databases"]:
        relative = Path(item["file"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe database path in manifest")
        path = _safe(snapshot / relative)
        if _hash(path) != item["sha256"]:
            raise ValueError("backup database checksum mismatch")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("backup database integrity failure")
    return manifest


def prune_managed(root: Path, instance: str, *, keep_state: int = 28, keep_full: int = 7) -> list[str]:
    if min(keep_state, keep_full) < 2:
        raise ValueError("at least two verified backups of each kind must be retained")
    managed = _safe(Path(root) / "backups" / "managed-v1" / instance)
    removed = []
    for mode, keep in (("state", keep_state), ("full", keep_full)):
        candidates = sorted((p for p in managed.iterdir() if _SNAPSHOT.fullmatch(p.name)
            and p.name.endswith("-" + mode) and p.is_dir() and not p.is_symlink()), reverse=True)
        if len(candidates) <= keep:
            continue
        # Prove two latest retained recovery points before deleting only this
        # tool's own completed snapshots. Historical/audit backups stay intact.
        for snapshot in candidates[:2]:
            verify_backup(snapshot)
        for snapshot in candidates[keep:]:
            manifest = json.loads((snapshot / "manifest.json").read_text())
            if manifest.get("format") == FORMAT and manifest.get("instance") == instance and manifest.get("mode") == mode:
                shutil.rmtree(snapshot)
                removed.append(snapshot.name)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/opt/qq-bots"))
    parser.add_argument("--instance", choices=("carrot", "kona"))
    parser.add_argument("--mode", choices=("state", "full"), default="state")
    parser.add_argument("--extra-dir", type=Path, action="append", default=[])
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--prune", action="store_true")
    args = parser.parse_args()
    if args.verify:
        manifest = verify_backup(args.verify)
        print(json.dumps({"verified": str(args.verify), "databases": len(manifest["databases"])}))
        return 0
    if not args.instance:
        parser.error("--instance is required when creating a backup")
    snapshot = create_backup(args.root, args.instance, mode=args.mode, extra_dirs=tuple(args.extra_dir))
    removed = prune_managed(args.root, args.instance) if args.prune else []
    print(json.dumps({"backup": str(snapshot), "verified": True, "pruned_managed_snapshots": removed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
