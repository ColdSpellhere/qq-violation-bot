#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
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


def _text_at(path: str, ref: str | None = None) -> str | None:
    if ref:
        raw = _git("show", f"{ref}:{path}", text=False).stdout
    else:
        raw = (ROOT / path).read_bytes()
    if b"\x00" in raw:
        return None
    return raw.decode("utf-8", errors="replace")


def scan_ref(ref: str | None, runtime_values: dict[str, str]) -> list[str]:
    findings: list[str] = []
    prefix = f"{ref}:" if ref else ""
    for path in _tracked_paths(ref):
        text = _text_at(path, ref)
        if text is None:
            continue
        label = f"{prefix}{path}"
        findings.extend(generic_findings(label, text))
        findings.extend(runtime_findings(label, text, runtime_values))
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
        for revision in revisions():
            findings.extend(scan_ref(revision, runtime_values))
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
