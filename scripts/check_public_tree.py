#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
SENSITIVE_KEYS = (
    "TARGET_GROUP_ID",
    "BOT_SELF_ID",
    "NAPCAT_ACCESS_TOKEN",
    "AI_API_KEY",
    "ADMIN_SEED",
)
TOKEN_RE = re.compile(r"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,})")
PRIVATE_KEY_RE = re.compile(r"BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY")


@dataclass(frozen=True)
class HistoricalBaselineEntry:
    path: str
    blob_oid: str
    finding_class: str
    reason: str
    reviewed_on: str


HISTORICAL_BASELINE = (
    HistoricalBaselineEntry(
        path="tests/test_private_memory_processing.py",
        blob_oid="39603b010b2c564517180ff7d15577df291707ff",
        finding_class="generic API token",
        reason="Reviewed synthetic secret-filter regression fixture removed from the current tree.",
        reviewed_on="2026-08-23",
    ),
)


def _git(*args: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=text,
    )


def generic_findings(path: str, text: str) -> list[str]:
    findings: list[str] = []
    if TOKEN_RE.search(text):
        findings.append(f"{path}: generic API token")
    if PRIVATE_KEY_RE.search(text):
        findings.append(f"{path}: private key material")
    return findings


def runtime_findings(path: str, text: str, values: dict[str, str]) -> list[str]:
    return [
        f"{path}: runtime value for {key}"
        for key, value in values.items()
        if value and value in text
    ]


def filter_historical_findings(
    path: str, blob_oid: str, findings: Iterable[str]
) -> list[str]:
    allowed_classes = {
        entry.finding_class
        for entry in HISTORICAL_BASELINE
        if entry.path == path and entry.blob_oid == blob_oid
    }
    prefix = f"{path}: "
    return [
        finding
        for finding in findings
        if not (
            finding.startswith(prefix)
            and finding[len(prefix):] in allowed_classes
        )
    ]


def scan_text(
    path: str,
    text: str,
    runtime_values: dict[str, str],
    *,
    blob_oid: str = "",
    historical: bool = False,
) -> list[str]:
    findings = generic_findings(path, text)
    findings.extend(runtime_findings(path, text, runtime_values))
    if historical:
        return filter_historical_findings(path, blob_oid, findings)
    return findings


def _runtime_values() -> dict[str, str]:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return {}
    parsed = dotenv_values(env_path)
    return {
        key: str(parsed.get(key) or "").strip()
        for key in SENSITIVE_KEYS
        if str(parsed.get(key) or "").strip()
    }


def _tracked_paths(ref: str | None = None) -> list[str]:
    if ref:
        output = _git("ls-tree", "-r", "--name-only", ref).stdout
    else:
        output = _git("ls-files").stdout
    return [line for line in output.splitlines() if line]


def _tracked_entries(ref: str) -> list[tuple[str, str]]:
    output = _git("ls-tree", "-r", "-z", ref).stdout
    entries: list[tuple[str, str]] = []
    for item in output.split("\0"):
        if not item:
            continue
        metadata, path = item.split("\t", 1)
        _mode, object_type, oid = metadata.split(" ", 2)
        if object_type == "blob":
            entries.append((path, oid))
    return entries


def _text_at(path: str, ref: str | None = None) -> str | None:
    if ref:
        raw = _git("show", f"{ref}:{path}", text=False).stdout
    else:
        raw = (ROOT / path).read_bytes()
    if b"\x00" in raw:
        return None
    return raw.decode("utf-8", errors="replace")


def scan_ref(
    ref: str | None,
    runtime_values: dict[str, str],
    *,
    seen_history: set[tuple[str, str]] | None = None,
) -> list[str]:
    findings: list[str] = []
    if ref is None:
        entries = ((path, "") for path in _tracked_paths())
    else:
        entries = _tracked_entries(ref)
    for path, blob_oid in entries:
        if ref is not None and seen_history is not None:
            identity = (path, blob_oid)
            if identity in seen_history:
                continue
            seen_history.add(identity)
        text = _text_at(path, ref) if ref is None else _git(
            "cat-file", "blob", blob_oid, text=False
        ).stdout
        if isinstance(text, bytes):
            if b"\x00" in text:
                continue
            text = text.decode("utf-8", errors="replace")
        if text is None:
            continue
        item_findings = scan_text(
            path,
            text,
            runtime_values,
            blob_oid=blob_oid,
            historical=ref is not None,
        )
        if ref is not None:
            item_findings = [f"{ref}:{finding}" for finding in item_findings]
        findings.extend(item_findings)
    return findings


def revisions() -> Iterable[str]:
    return _git("rev-list", "--all").stdout.splitlines()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    runtime_values = _runtime_values()
    findings = scan_ref(None, runtime_values)
    if args.history:
        seen_history: set[tuple[str, str]] = set()
        for revision in revisions():
            findings.extend(
                scan_ref(revision, runtime_values, seen_history=seen_history)
            )
    unique = list(dict.fromkeys(findings))
    for finding in unique:
        print(finding)
    if unique:
        print(f"public source scan: FAIL ({len(unique)} findings)")
        return 1
    print("public source scan: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
