from __future__ import annotations

import json
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
MAX_ALIASES = 8
MAX_TRAITS = 8
SENSITIVE_RE = re.compile(
    r"手机号|电话号码?|身份证|住址|家庭地址|密码|token|银行卡|微信号|邮箱|真实姓名|\d{6,}",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MemoryTrait:
    text: str
    evidence_message_id: str
    updated_at: str


@dataclass(frozen=True)
class MemberProfile:
    group_id: int
    user_id: str
    nickname: str
    aliases: tuple[str, ...]
    traits: tuple[MemoryTrait, ...]
    updated_at: str


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _decode_profile(row: sqlite3.Row | tuple) -> MemberProfile:
    group_id, user_id, nickname, aliases_json, traits_json, updated_at = row
    try:
        aliases = tuple(str(item) for item in json.loads(aliases_json) if str(item).strip())
    except (TypeError, json.JSONDecodeError):
        aliases = ()
    try:
        traits = tuple(MemoryTrait(**item) for item in json.loads(traits_json) if isinstance(item, dict))
    except (TypeError, json.JSONDecodeError, KeyError):
        traits = ()
    return MemberProfile(int(group_id), str(user_id), str(nickname), aliases, traits, str(updated_at))


def _profile_row(conn: sqlite3.Connection, group_id: int, user_id: str) -> MemberProfile | None:
    row = conn.execute(
        "SELECT group_id,user_id,nickname,aliases_json,traits_json,updated_at "
        "FROM member_memories WHERE group_id=? AND user_id=?",
        (group_id, user_id),
    ).fetchone()
    return _decode_profile(row) if row else None


def _write_mirror(root: Path, profile: MemberProfile) -> None:
    directory = root / str(profile.group_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{profile.user_id}.json"
    temporary = directory / f".{profile.user_id}.json.tmp"
    payload = {
        "group_id": profile.group_id,
        "user_id": profile.user_id,
        "nickname": profile.nickname,
        "aliases": list(profile.aliases),
        "traits": [asdict(item) for item in profile.traits],
        "updated_at": profile.updated_at,
    }
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, target)
    target.chmod(0o600)


def remember_identity(
    path: Path,
    root: Path,
    *,
    group_id: int,
    user_id: str,
    nickname: str,
) -> MemberProfile:
    cleaned_name = nickname.strip() or str(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(MEMORY_SCHEMA)
        existing = _profile_row(conn, group_id, str(user_id))
        aliases = list(existing.aliases if existing else ())
        if existing and existing.nickname != cleaned_name and existing.nickname not in aliases:
            aliases.append(existing.nickname)
        aliases = [item for item in aliases if item != cleaned_name][-MAX_ALIASES:]
        traits = existing.traits if existing else ()
        updated_at = _now()
        conn.execute(
            """
            INSERT INTO member_memories(group_id,user_id,nickname,aliases_json,traits_json,updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(group_id,user_id) DO UPDATE SET
              nickname=excluded.nickname,aliases_json=excluded.aliases_json,updated_at=excluded.updated_at
            """,
            (
                group_id,
                str(user_id),
                cleaned_name,
                json.dumps(aliases, ensure_ascii=False),
                json.dumps([asdict(item) for item in traits], ensure_ascii=False),
                updated_at,
            ),
        )
        profile = _profile_row(conn, group_id, str(user_id))
    assert profile is not None
    _write_mirror(root, profile)
    return profile


def load_profiles(path: Path, *, group_id: int, user_ids: Iterable[str]) -> list[MemberProfile]:
    ordered = list(dict.fromkeys(str(item) for item in user_ids if str(item)))
    if not path.is_file() or not ordered:
        return []
    try:
        with sqlite3.connect(path) as conn:
            conn.executescript(MEMORY_SCHEMA)
            profiles = [_profile_row(conn, group_id, item) for item in ordered]
    except (OSError, sqlite3.Error):
        return []
    return [item for item in profiles if item is not None]


def apply_candidates(
    path: Path,
    root: Path,
    *,
    group_id: int,
    context: Sequence[ContextMessage],
    candidates: Sequence[Mapping[str, object]],
) -> int:
    evidence = {item.message_id: item for item in context if item.message_id and item.user_id}
    valid: list[tuple[str, str, str]] = []
    accepted_per_user: dict[str, int] = {}
    for candidate in candidates:
        user_id = str(candidate.get("user_id") or "").strip()
        trait = str(candidate.get("trait") or "").strip()
        evidence_id = str(candidate.get("evidence_message_id") or "").strip()
        quote = str(candidate.get("quote") or "").strip()
        source = evidence.get(evidence_id)
        if (
            source is None
            or source.user_id != user_id
            or not quote
            or quote not in source.text
            or not (2 <= len(trait) <= 80)
            or SENSITIVE_RE.search(trait)
            or SENSITIVE_RE.search(quote)
        ):
            continue
        if accepted_per_user.get(user_id, 0) >= MAX_TRAITS:
            continue
        valid.append((user_id, trait, evidence_id))
        accepted_per_user[user_id] = accepted_per_user.get(user_id, 0) + 1
    applied = 0
    for user_id, trait, evidence_id in valid:
        source = evidence[evidence_id]
        profile = remember_identity(
            path,
            root,
            group_id=group_id,
            user_id=user_id,
            nickname=source.nickname,
        )
        traits = [item for item in profile.traits if item.text != trait]
        traits.append(MemoryTrait(trait, evidence_id, _now()))
        traits = traits[-MAX_TRAITS:]
        updated_at = _now()
        with sqlite3.connect(path) as conn:
            conn.executescript(MEMORY_SCHEMA)
            conn.execute(
                "UPDATE member_memories SET traits_json=?,updated_at=? WHERE group_id=? AND user_id=?",
                (json.dumps([asdict(item) for item in traits], ensure_ascii=False), updated_at, group_id, user_id),
            )
            updated = _profile_row(conn, group_id, user_id)
        assert updated is not None
        _write_mirror(root, updated)
        applied += 1
    return applied
