import json
import re
from difflib import SequenceMatcher
from typing import Any

from .db import connect, now_str, row_to_dict, rows_to_dicts


def _load_aliases(raw: str | None) -> list[str]:
    try:
        aliases = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(aliases, list):
        return []
    cleaned: list[str] = []
    for item in aliases:
        alias = str(item).strip()
        if alias and alias not in cleaned:
            cleaned.append(alias)
    return cleaned


def _dump_aliases(aliases: list[str]) -> str:
    return json.dumps(aliases, ensure_ascii=False)


def _merge_aliases(raw: str | None, candidates: list[str | None], primary_name: str) -> str:
    aliases = _load_aliases(raw)
    for item in candidates:
        alias = (item or "").strip()
        if not alias or alias == primary_name or alias in aliases:
            continue
        aliases.append(alias)
    return _dump_aliases(aliases)


def _normalize_name(value: str | None) -> str:
    return re.sub(r"\s+", "", (value or "").casefold())


def _admin_names(admin: dict[str, Any]) -> list[str]:
    names = [admin.get("nickname") or "", admin.get("qq_number") or ""]
    names.extend(_load_aliases(admin.get("aliases")))
    return names


def _score(query: str, admin: dict[str, Any]) -> float:
    names = _admin_names(admin)
    best = 0.0
    query = query.strip()
    query_norm = _normalize_name(query)
    for name in names:
        if not name:
            continue
        name_norm = _normalize_name(name)
        if query == name or (query_norm and query_norm == name_norm):
            best = max(best, 1.0)
        elif query_norm and (query_norm in name_norm or name_norm in query_norm):
            best = max(best, 0.85)
        else:
            best = max(best, SequenceMatcher(None, query_norm, name_norm).ratio())
    return best


def grant_admin(qq_number: str, nickname: str | None = None) -> dict[str, Any]:
    qq_number = str(qq_number).strip()
    incoming_name = (nickname or "").strip()
    if incoming_name == qq_number:
        incoming_name = ""
    ts = now_str()
    with connect() as conn:
        existing = row_to_dict(conn.execute("SELECT * FROM admins WHERE qq_number=?", (qq_number,)).fetchone())
        if existing:
            display_name = incoming_name or existing["nickname"] or qq_number
            aliases = _merge_aliases(existing.get("aliases"), [existing.get("nickname")], display_name)
            conn.execute(
                """
                UPDATE admins
                SET nickname=?, aliases=?, is_active=1, updated_at=?
                WHERE qq_number=?
                """,
                (display_name, aliases, ts, qq_number),
            )
        else:
            display_name = incoming_name or qq_number
            conn.execute(
                """
                INSERT INTO admins(qq_number, nickname, aliases, is_active, created_at, updated_at)
                VALUES(?, ?, '[]', 1, ?, ?)
                """,
                (qq_number, display_name, ts, ts),
            )
        return row_to_dict(conn.execute("SELECT * FROM admins WHERE qq_number=?", (qq_number,)).fetchone()) or {}


def grant_admins(admins: list[tuple[str, str | None]]) -> int:
    if not admins:
        return 0
    cleaned: list[tuple[str, str]] = []
    seen: set[str] = set()
    for qq_number, nickname in admins:
        qq = str(qq_number).strip()
        if not qq or qq in seen:
            continue
        seen.add(qq)
        cleaned.append((qq, (nickname or "").strip()))
    if not cleaned:
        return 0
    ts = now_str()
    with connect() as conn:
        for qq, nickname in cleaned:
            existing = row_to_dict(conn.execute("SELECT * FROM admins WHERE qq_number=?", (qq,)).fetchone())
            if nickname == qq:
                nickname = ""
            if existing:
                display_name = nickname or existing["nickname"] or qq
                aliases = _merge_aliases(existing.get("aliases"), [existing.get("nickname")], display_name)
                conn.execute(
                    """
                    UPDATE admins
                    SET nickname=?, aliases=?, is_active=1, updated_at=?
                    WHERE qq_number=?
                    """,
                    (display_name, aliases, ts, qq),
                )
            else:
                display_name = nickname or qq
                conn.execute(
                    """
                    INSERT INTO admins(qq_number, nickname, aliases, is_active, created_at, updated_at)
                    VALUES(?, ?, '[]', 1, ?, ?)
                    """,
                    (qq, display_name, ts, ts),
                )
    return len(cleaned)


def resolve_admin_by_qq(qq_number: str | None) -> tuple[str, Any]:
    qq = str(qq_number or "").strip()
    if not qq:
        return "missing", None
    with connect() as conn:
        row = conn.execute("SELECT * FROM admins WHERE qq_number=? AND is_active=1", (qq,)).fetchone()
        if row:
            return "ok", dict(row)
    return "not_found", None


def resolve_operator(qq_number: str, fallback_nickname: str | None = None) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM admins WHERE qq_number=? AND is_active=1", (qq_number,)).fetchone()
        if row:
            current = dict(row)
            if fallback_nickname and fallback_nickname.strip() and fallback_nickname.strip() != current.get("nickname"):
                return grant_admin(qq_number, fallback_nickname)
            return current
    return grant_admin(qq_number, fallback_nickname)


def resolve_admin_by_name(name: str | None, default_admin: dict[str, Any] | None = None) -> tuple[str, Any]:
    name = (name or "").strip()
    if not name:
        return ("ok", default_admin) if default_admin else ("missing", None)
    with connect() as conn:
        rows = rows_to_dicts(conn.execute("SELECT * FROM admins WHERE is_active=1").fetchall())
    scored = [(a, _score(name, a)) for a in rows]
    ordered = [(a, score) for a, score in sorted(scored, key=lambda x: x[1], reverse=True) if score >= 0.55]
    exact = [a for a, score in ordered if score >= 1.0]
    if len(exact) == 1:
        return "ok", exact[0]
    strong = [a for a, score in ordered if score >= 0.85]
    if len(strong) == 1:
        return "ok", strong[0]
    matches = [a for a, _score_value in ordered]
    if len(matches) == 1:
        return "ok", matches[0]
    if len(matches) > 1:
        return "ambiguous", matches[:8]
    return "not_found", None
