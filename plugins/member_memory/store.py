from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Mapping, Sequence

if TYPE_CHECKING:
    from plugins.chat_archive.db import ContextMessage

MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS member_memories (
    group_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    nickname TEXT NOT NULL,
    aliases_json TEXT NOT NULL,
    traits_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(group_id,user_id)
);
"""
MEMORY_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS member_memory_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    trait TEXT NOT NULL,
    evidence_message_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(group_id,user_id,trait,evidence_message_id)
);
CREATE INDEX IF NOT EXISTS idx_member_memory_facts_member
ON member_memory_facts(group_id,user_id,id);
CREATE TABLE IF NOT EXISTS member_memory_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    UNIQUE(group_id,user_id,alias)
);
CREATE TABLE IF NOT EXISTS member_memory_summaries (
    group_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    through_fact_id INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(group_id,user_id)
);
"""
LEGACY_VIEW_LIMIT = 8
MAX_ALIASES = LEGACY_VIEW_LIMIT
MAX_TRAITS = LEGACY_VIEW_LIMIT
PROMPT_ALIAS_LIMIT = 5
PROMPT_UNSUMMARIZED_LIMIT = 8
logger = logging.getLogger(__name__)

SENSITIVE_RE = re.compile(
    r"手机号|电话号码?|身份证|住址|家庭地址|密码|token|银行卡|微信号|邮箱|真实姓名|\d{6,}",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MemoryTrait:
    text: str
    evidence_message_id: str
    updated_at: str
    fact_id: int = 0


@dataclass(frozen=True)
class MemberProfile:
    group_id: int
    user_id: str
    nickname: str
    aliases: tuple[str, ...]
    traits: tuple[MemoryTrait, ...]
    updated_at: str
    summary: str = ""
    summary_through_fact_id: int = 0


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(MEMORY_SCHEMA)
    conn.executescript(MEMORY_LEDGER_SCHEMA)


def _decode_json_aliases(value: object) -> tuple[str, ...]:
    try:
        return tuple(str(item) for item in json.loads(str(value)) if str(item).strip())
    except (TypeError, json.JSONDecodeError):
        return ()


def _decode_json_traits(value: object) -> tuple[MemoryTrait, ...]:
    try:
        return tuple(
            MemoryTrait(
                text=str(item["text"]),
                evidence_message_id=str(item["evidence_message_id"]),
                updated_at=str(item["updated_at"]),
                fact_id=int(item.get("fact_id", 0)),
            )
            for item in json.loads(str(value))
            if isinstance(item, dict) and {"text", "evidence_message_id", "updated_at"} <= item.keys()
        )
    except (TypeError, json.JSONDecodeError, KeyError):
        return ()


def _profile_row(conn: sqlite3.Connection, group_id: int, user_id: str) -> MemberProfile | None:
    row = conn.execute(
        "SELECT group_id,user_id,nickname,aliases_json,traits_json,updated_at "
        "FROM member_memories WHERE group_id=? AND user_id=?",
        (group_id, user_id),
    ).fetchone()
    if row is None:
        return None
    aliases = tuple(
        item[0]
        for item in conn.execute(
            "SELECT alias FROM member_memory_aliases WHERE group_id=? AND user_id=? ORDER BY id",
            (group_id, user_id),
        ).fetchall()
    )
    if not aliases:
        aliases = _decode_json_aliases(row[3])
    facts = tuple(
        MemoryTrait(text, evidence, created, int(fact_id))
        for fact_id, text, evidence, created in conn.execute(
            "SELECT id,trait,evidence_message_id,created_at FROM member_memory_facts "
            "WHERE group_id=? AND user_id=? ORDER BY id",
            (group_id, user_id),
        ).fetchall()
    )
    traits = facts or _decode_json_traits(row[4])
    summary_row = conn.execute(
        "SELECT summary_text,through_fact_id FROM member_memory_summaries WHERE group_id=? AND user_id=?",
        (group_id, user_id),
    ).fetchone()
    return MemberProfile(
        int(row[0]), str(row[1]), str(row[2]), aliases, traits, str(row[5]),
        str(summary_row[0]) if summary_row else "",
        int(summary_row[1]) if summary_row else 0,
    )


def _import_legacy_profile(conn: sqlite3.Connection, profile: MemberProfile) -> None:
    for alias in profile.aliases:
        _append_alias(conn, profile.group_id, profile.user_id, alias, profile.updated_at)
    for trait in profile.traits:
        _append_fact(
            conn,
            profile.group_id,
            profile.user_id,
            trait.text,
            trait.evidence_message_id,
            trait.updated_at,
        )


def _append_alias(conn: sqlite3.Connection, group_id: int, user_id: str, alias: str, seen_at: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO member_memory_aliases(group_id,user_id,alias,first_seen_at) VALUES(?,?,?,?)",
        (group_id, user_id, alias, seen_at),
    )


def _append_fact(
    conn: sqlite3.Connection, group_id: int, user_id: str, trait: str, evidence_id: str, created_at: str
) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO member_memory_facts(group_id,user_id,trait,evidence_message_id,created_at) "
        "VALUES(?,?,?,?,?)",
        (group_id, user_id, trait, evidence_id, created_at),
    )
    return cursor.rowcount == 1


def _write_mirror(path: Path, root: Path, group_id: int, user_id: str) -> None:
    with sqlite3.connect(path) as conn:
        _ensure_schema(conn)
        profile = _profile_row(conn, group_id, str(user_id))
    if profile is None:
        return
    directory = root / str(profile.group_id)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("member memory mirror directory creation failed for group=%s user=%s", group_id, user_id)
        return
    target = directory / f"{profile.user_id}.json"
    temporary = directory / f".{profile.user_id}.json.tmp"
    payload = {
        "group_id": profile.group_id,
        "user_id": profile.user_id,
        "nickname": profile.nickname,
        "aliases": list(profile.aliases),
        "traits": [asdict(item) for item in profile.traits],
        "summary": profile.summary,
        "summary_through_fact_id": profile.summary_through_fact_id,
        "updated_at": profile.updated_at,
    }
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, target)
        target.chmod(0o600)
    except OSError:
        logger.exception("member memory mirror write failed for group=%s user=%s", group_id, user_id)


def remember_identity(path: Path, root: Path, *, group_id: int, user_id: str, nickname: str) -> MemberProfile:
    cleaned_name = nickname.strip() or str(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    user_id = str(user_id)
    with sqlite3.connect(path) as conn:
        _ensure_schema(conn)
        existing = _profile_row(conn, group_id, user_id)
        if existing is not None:
            _import_legacy_profile(conn, existing)
            existing = _profile_row(conn, group_id, user_id)
        seen_at = _now()
        if existing and existing.nickname != cleaned_name:
            _append_alias(conn, group_id, user_id, existing.nickname, seen_at)
        updated_at = seen_at
        aliases = tuple(
            item[0]
            for item in conn.execute(
                "SELECT alias FROM member_memory_aliases WHERE group_id=? AND user_id=? ORDER BY id",
                (group_id, user_id),
            ).fetchall()
        )
        traits = existing.traits if existing else ()
        conn.execute(
            "INSERT INTO member_memories(group_id,user_id,nickname,aliases_json,traits_json,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(group_id,user_id) DO UPDATE SET "
            "nickname=excluded.nickname,aliases_json=excluded.aliases_json,updated_at=excluded.updated_at",
            (
                group_id, user_id, cleaned_name,
                json.dumps(list(aliases[-LEGACY_VIEW_LIMIT:]), ensure_ascii=False),
                json.dumps([asdict(item) for item in traits[-LEGACY_VIEW_LIMIT:]], ensure_ascii=False),
                updated_at,
            ),
        )
        profile = _profile_row(conn, group_id, user_id)
    assert profile is not None
    _write_mirror(path, root, group_id, user_id)
    return profile


def load_profiles(path: Path, *, group_id: int, user_ids: Iterable[str]) -> list[MemberProfile]:
    ordered = list(dict.fromkeys(str(item) for item in user_ids if str(item)))
    if not path.is_file() or not ordered:
        return []
    try:
        with sqlite3.connect(path) as conn:
            _ensure_schema(conn)
            profiles = [_profile_row(conn, group_id, item) for item in ordered]
    except (OSError, sqlite3.Error):
        return []
    return [item for item in profiles if item is not None]


def apply_candidates(
    path: Path, root: Path, *, group_id: int, context: Sequence[ContextMessage],
    candidates: Sequence[Mapping[str, object]],
) -> int:
    evidence = {item.message_id: item for item in context if item.message_id and item.user_id}
    valid: list[tuple[str, str, str]] = []
    for candidate in candidates:
        user_id = str(candidate.get("user_id") or "").strip()
        trait = str(candidate.get("trait") or "").strip()
        evidence_id = str(candidate.get("evidence_message_id") or "").strip()
        quote = str(candidate.get("quote") or "").strip()
        source = evidence.get(evidence_id)
        if (
            source is None or source.user_id != user_id or not quote or quote not in source.text
            or not (2 <= len(trait) <= 80) or SENSITIVE_RE.search(trait) or SENSITIVE_RE.search(quote)
        ):
            continue
        valid.append((user_id, trait, evidence_id))
    applied = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    for user_id, trait, evidence_id in valid:
        source = evidence[evidence_id]
        remember_identity(path, root, group_id=group_id, user_id=user_id, nickname=source.nickname)
        with sqlite3.connect(path) as conn:
            _ensure_schema(conn)
            if not _append_fact(conn, group_id, user_id, trait, evidence_id, _now()):
                continue
            facts = [
                MemoryTrait(text, evidence, created, int(fact_id))
                for fact_id, text, evidence, created in conn.execute(
                    "SELECT id,trait,evidence_message_id,created_at FROM member_memory_facts "
                    "WHERE group_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
                    (group_id, user_id, LEGACY_VIEW_LIMIT),
                ).fetchall()
            ][::-1]
            updated_at = _now()
            conn.execute(
                "UPDATE member_memories SET traits_json=?,updated_at=? WHERE group_id=? AND user_id=?",
                (json.dumps([asdict(item) for item in facts], ensure_ascii=False), updated_at, group_id, user_id),
            )
        _write_mirror(path, root, group_id, user_id)
        applied += 1
    return applied
