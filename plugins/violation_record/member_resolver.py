import json
from difflib import SequenceMatcher
from typing import Any

from .db import connect, now_str, row_to_dict, rows_to_dicts


def _score(query: str, name: str | None, aliases: str | None = None) -> float:
    if not query or not name:
        return 0
    names = [name]
    try:
        names.extend(json.loads(aliases or "[]"))
    except json.JSONDecodeError:
        pass
    best = 0.0
    for item in names:
        if not item:
            continue
        if query == item:
            best = max(best, 1.0)
        elif query in item or item in query:
            best = max(best, 0.85)
        else:
            best = max(best, SequenceMatcher(None, query, item).ratio())
    return best


def get_member_by_id(member_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        return row_to_dict(conn.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone())


def get_or_create_member(qq_number: str, qq_nickname: str | None) -> dict[str, Any]:
    ts = now_str()
    nickname = qq_nickname or "未知昵称"
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO members(qq_number, qq_nickname, aliases, created_at, updated_at)
            VALUES(?, ?, '[]', ?, ?)
            ON CONFLICT(qq_number) DO UPDATE SET
                qq_nickname=COALESCE(NULLIF(excluded.qq_nickname, '未知昵称'), members.qq_nickname),
                updated_at=excluded.updated_at
            """,
            (qq_number, nickname, ts, ts),
        )
        return row_to_dict(conn.execute("SELECT * FROM members WHERE qq_number=?", (qq_number,)).fetchone()) or {}


def resolve_member(qq_number: str | None, qq_nickname: str | None, allow_create: bool = False) -> tuple[str, Any]:
    if qq_number:
        with connect() as conn:
            row = conn.execute("SELECT * FROM members WHERE qq_number=?", (qq_number,)).fetchone()
        if row:
            return "ok", dict(row)
        if allow_create and qq_nickname:
            return "ok", get_or_create_member(qq_number, qq_nickname)
        return "need_member_info", None
    if not qq_nickname:
        return "missing", None
    with connect() as conn:
        rows = rows_to_dicts(conn.execute("SELECT * FROM members").fetchall())
    scored = [(m, _score(qq_nickname, m.get("qq_nickname"), m.get("aliases"))) for m in rows]
    matches = [m for m, score in sorted(scored, key=lambda x: x[1], reverse=True) if score >= 0.55]
    if len(matches) == 1:
        return "ok", matches[0]
    if len(matches) > 1:
        return "ambiguous", matches[:8]
    return "not_found", None


def format_member(member: dict[str, Any] | None) -> str:
    if not member:
        return "未知昵称（未知QQ）"
    nickname = member.get("qq_nickname") or "未知昵称"
    return f"{nickname}（{member.get('qq_number')}）"
