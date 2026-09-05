#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

sys.dont_write_bytecode = True
if __package__:
    from .ops_runtime import read_environment
else:
    from ops_runtime import read_environment


ROOT = Path(__file__).resolve().parents[1]
SENSITIVE_KEYS = (
    "TARGET_GROUP_ID",
    "BOT_SELF_ID",
    "NAPCAT_ACCESS_TOKEN",
    "AI_API_KEY",
    "GLM_API_KEY",
    "TAVILY_API_KEY",
    "ADMIN_SEED",
    "SUPERUSERS",
    "GROUP_CHAT_ALLOWED_GROUP_IDS",
    "PRIVATE_CHAT_ALLOWED_USER_IDS",
    "PRIVATE_CHAT_ALLOWED_USER_ID",
    "HIVE_MEMBER_MONITOR_GROUP_ID",
    "HIVE_MEMBER_MONITOR_GROUP_IDS",
    "HIVE_MEMBER_MONITOR_GROUP_LABELS_JSON",
    "HIVE_MEMBER_REPORT_GROUP_ID",
    "MONITOR_ONLY_GROUP_IDS",
    "CONTENT_ALERT_SOURCE_GROUP_IDS",
    "CONTENT_ALERT_REPORT_GROUP_ID",
)
TOKEN_RE = re.compile(r"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,})")
GLM_TOKEN_RE = re.compile(r"[A-Fa-f0-9]{32}\.[A-Za-z0-9_-]{16,}")
TAVILY_TOKEN_RE = re.compile(r"tvly-(?:dev|prod)-[A-Za-z0-9_-]{20,}")
PRIVATE_KEY_RE = re.compile(r"BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY")
TOKEN_BYTES_RE = re.compile(rb"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,})")
GLM_TOKEN_BYTES_RE = re.compile(rb"[A-Fa-f0-9]{32}\.[A-Za-z0-9_-]{16,}")
TAVILY_TOKEN_BYTES_RE = re.compile(rb"tvly-(?:dev|prod)-[A-Za-z0-9_-]{20,}")
PRIVATE_KEY_BYTES_RE = re.compile(rb"BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY")
MAX_SCAN_BYTES = 20 * 1024 * 1024
_DATABASE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")


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
    if GLM_TOKEN_RE.search(text):
        findings.append(f"{path}: GLM API token")
    if TAVILY_TOKEN_RE.search(text):
        findings.append(f"{path}: Tavily API token")
    if PRIVATE_KEY_RE.search(text):
        findings.append(f"{path}: private key material")
    return findings


def path_findings(path: str) -> list[str]:
    normalized = path.replace("\\", "/")
    while normalized.startswith('./'):
        normalized = normalized[2:]
    lower = normalized.lower()
    parts = lower.split('/')
    if any((part == '.env' or part.startswith('.env.')) and part != '.env.example' for part in parts):
        return [f'{path}: private environment file']
    if 'data/content_alert/' in lower:
        return [f"{path}: content alert rule data"]
    if lower.endswith(_DATABASE_SUFFIXES) or any(
        marker in lower for marker in (".db-", ".sqlite-", '.sqlite3-')
    ):
        return [f"{path}: SQLite database"]
    if parts[-1] in {'runtime_features.json', 'runtime_features.json.bak'}:
        return [f"{path}: runtime feature state"]
    if 'exports' in parts:
        return [f"{path}: export artifact"]
    if lower.endswith(_IMAGE_SUFFIXES):
        return [f"{path}: image artifact"]
    if lower.endswith(".log") or 'logs' in parts:
        return [f"{path}: log artifact"]
    if any(part in {'backups', 'instances', '.venv'} for part in parts):
        return [f'{path}: private runtime directory']
    if 'data' in parts and parts[-1] not in {'readme.md', '.gitkeep', '.gitignore'}:
        return [f'{path}: private runtime data']
    return []


def assignment_findings(path: str, text: str) -> list[str]:
    """Detect literal credential/config assignments without private runtime inputs."""
    keys = '|'.join(re.escape(key) for key in SENSITIVE_KEYS)
    expression = re.compile(r'(?<![\w$])(?:[\"\']?)(' + keys + r')(?:[\"\']?)[ \t]*(?::(?![?=-])|=(?![=~]))[ \t]*')
    findings = []
    for match in expression.finditer(text):
        tail = text[match.end():].split('\n', 1)[0]
        if tail.startswith(('"', "'")):
            quoted = re.match(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' ''', tail, re.X)
            if not quoted:
                continue  # source string boundary, not a complete literal value
            try:
                value = str(ast.literal_eval(quoted[0]))
            except (ValueError, SyntaxError):
                continue
        elif tail.startswith(('{', '[')):
            try:
                parsed, _length = json.JSONDecoder().raw_decode(tail)
                identifiers = list(parsed) if isinstance(parsed, (dict, list)) else []
                if all(_placeholder_identifier(str(value), path) for value in identifiers):
                    continue
            except ValueError:
                pass
            value = tail
        else:
            token = re.match(r'[^\s,#}\)\"\'`]+', tail)
            value = token[0] if token else ''
        if path.endswith('.py') and not tail.startswith(('"', "'")) and (
                re.match(r'^[A-Za-z_]\w*(?:\(|\.|$)', value) or value == '('):
            continue  # variable/call expression in Python, not a secret literal
        # Only explicit documentation/test placeholders are accepted. Dynamic
        # source expressions are not literal assignments; no values are printed.
        placeholder = (not value or value in {'[]', '{}', '0', 'None', 'null', '""', "''"}
            or all(_placeholder_identifier(item.strip(), path) for item in value.split(','))
            or (match[1] == 'ADMIN_SEED' and all(_placeholder_identifier(item.split(':', 1)[0], path) for item in value.split(';')))
            or re.match(r'(?i)^(?:replace[-_]|your[-_]|example[-_]|synthetic[-_]|test[-_]|dummy[-_]|placeholder|你的|<|\$\{|\$\(|\$[A-Z_])', value)
            or re.match(r'^(?:os\.|values\.|environment\.|str\(|_\w+\(|CONFIG\.|config\.|None\b|\s*\+|\{[A-Za-z_]\w*\}|\\d)', value))
        if not placeholder:
            findings.append(f'{path}: non-placeholder assignment for {match[1]}')
    return list(dict.fromkeys(findings))


def _placeholder_identifier(value: str, path: str = '') -> bool:
    # Existing public test/example constants, narrowly scoped to their files;
    # these exemptions do not apply to config files or arbitrary new paths.
    reviewed = {
        'README.md': {'123456', '654321'},
        'tests/test_chat_prompt_builder.py': {'817263541'},
        'tests/test_llm_gateway_lifecycle.py': {'918273645'},
        'tests/test_feature_control.py': {'222333444', '333444555'},
        'tests/test_instance_config.py': {'987654321'},
        'docs/superpowers/plans/2026-08-18-help-text.md': {'135792468'},
        'docs/superpowers/plans/2026-08-18-merged-query-forward.md': {'135792468'},
        'docs/superpowers/plans/2026-08-22-llm-gateway-prompt-builder.md': {'918273645'},
        'docs/superpowers/plans/2026-08-23-chat-web-search-multi-reply.md': {'817263540'},
    }
    return ((value.isdigit() and int(value) < 10000)
            or re.fullmatch(r'12345678\d{1,2}|999000\d{3}', value) is not None
            or value in reviewed.get(path, set()))


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
    findings.extend(assignment_findings(path, text))
    findings.extend(runtime_findings(path, text, runtime_values))
    if historical:
        return filter_historical_findings(path, blob_oid, findings)
    return findings


def scan_bytes(
    path: str,
    raw: bytes,
    runtime_values: dict[str, str],
    *,
    blob_oid: str = "",
    historical: bool = False,
) -> list[str]:
    if len(raw) > MAX_SCAN_BYTES:
        return [f"{path}: file exceeds scan size limit"]
    findings: list[str] = []
    if TOKEN_BYTES_RE.search(raw):
        findings.append(f"{path}: generic API token")
    if GLM_TOKEN_BYTES_RE.search(raw):
        findings.append(f"{path}: GLM API token")
    if TAVILY_TOKEN_BYTES_RE.search(raw):
        findings.append(f"{path}: Tavily API token")
    if PRIVATE_KEY_BYTES_RE.search(raw):
        findings.append(f"{path}: private key material")
    findings.extend(
        f"{path}: runtime value for {key}"
        for key, value in runtime_values.items()
        if value and value.encode("utf-8") in raw
    )
    findings.extend(assignment_findings(path, raw.decode('utf-8', errors='replace')))
    if historical:
        return filter_historical_findings(path, blob_oid, findings)
    return findings


def _runtime_values() -> dict[str, str]:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return {}
    parsed = read_environment(env_path)
    values = {
        key: str(parsed.get(key) or "").strip()
        for key in SENSITIVE_KEYS
        if str(parsed.get(key) or "").strip()
    }
    for key, value in tuple(values.items()):
        if key == 'SUPERUSERS' or key.endswith('_IDS'):
            try:
                parsed_ids = json.loads(value)
            except ValueError:
                parsed_ids = value.split(',')
            if isinstance(parsed_ids, list):
                for index, item in enumerate(parsed_ids):
                    identifier = str(item).strip()
                    if identifier.isdigit() and len(identifier) >= 5:
                        values[f'{key}[{index}]'] = identifier
    return values


def _tracked_paths(ref: str | None = None) -> list[str]:
    if ref:
        output = _git("ls-tree", "-r", '-z', "--name-only", ref).stdout
    else:
        output = _git("ls-files", '-z').stdout
    return [line for line in output.split('\0') if line]


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


def _bytes_at(path: str, ref: str | None = None) -> bytes:
    if ref:
        return _git("show", f"{ref}:{path}", text=False).stdout
    return (ROOT / path).read_bytes()


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
        if ref is None and (ROOT/path).is_symlink():
            findings.append(f'{path}: tracked symbolic link')
            continue
        if ref is not None and seen_history is not None:
            identity = (path, blob_oid)
            if identity in seen_history:
                continue
            seen_history.add(identity)
        item_path_findings = path_findings(path)
        if ref is not None:
            item_path_findings = [
                f"{ref}:{finding}" for finding in item_path_findings
            ]
        findings.extend(item_path_findings)
        raw = _bytes_at(path, ref) if ref is None else _git(
            "cat-file", "blob", blob_oid, text=False
        ).stdout
        item_findings = scan_bytes(
            path,
            raw,
            runtime_values,
            blob_oid=blob_oid,
            historical=ref is not None,
        )
        if ref is not None:
            item_findings = [f"{ref}:{finding}" for finding in item_findings]
        findings.extend(item_findings)
    return findings


def revisions(commit_range: str | None = None) -> Iterable[str]:
    if commit_range is not None:
        if commit_range.startswith('-') or '..' not in commit_range:
            raise ValueError('commit range must use BASE..HEAD syntax')
        return _git('rev-list', commit_range, '--').stdout.splitlines()
    return _git("rev-list", "--all").stdout.splitlines()


def main() -> int:
    global ROOT
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", action="store_true")
    parser.add_argument('--range', dest='commit_range', help='also scan every introduced commit in BASE..HEAD')
    parser.add_argument('--repo', type=Path, help='repository to scan when running from stable bin')
    args = parser.parse_args()
    if args.repo is not None:
        ROOT = args.repo.resolve(strict=True)
    runtime_values = _runtime_values()
    findings = scan_ref(None, runtime_values)
    if args.history or args.commit_range:
        seen_history: set[tuple[str, str]] = set()
        for revision in revisions(args.commit_range if not args.history else None):
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
