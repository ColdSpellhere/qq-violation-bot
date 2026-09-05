"""Durable, bounded group memory processing on the shared job queue."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from plugins.chat_archive.db import ContextMessage
from .ai import extract_memory_candidates
from .store import (
    LEGACY_VIEW_LIMIT, MemoryTrait, SENSITIVE_RE, _append_fact, _ensure_schema,
    _now, _write_mirror, pending_summary_batch,
)
from .safety import contains_secret
from .summary import refresh_member_summary


def _read_batch(path, scope, first, target, limit, chars):
    with closing(sqlite3.connect(path)) as connection:
        progress = connection.execute(
            "SELECT through_message_id,version FROM group_fact_progress WHERE group_id=? AND user_id=?",
            (scope.group_id, scope.user_id),
        ).fetchone()
        through, version = (int(progress[0]), int(progress[1])) if progress else (max(0, first - 1), 0)
        rows = connection.execute(
            "SELECT rowid,message_id,plaintext,sender_json FROM chat_messages "
            "WHERE group_id=? AND user_id=? AND rowid>? AND rowid<=? "
            "AND trim(plaintext)<>'' AND substr(ltrim(plaintext),1,1)<>'/' ORDER BY rowid LIMIT ?",
            (scope.group_id, scope.user_id, through, target, limit),
        ).fetchall()
    selected = []
    for rowid, mid, text, sender_json in rows:
        if chars <= 0:
            break
        try:
            sender = json.loads(sender_json)
            name = str(sender.get('card') or sender.get('nickname') or scope.user_id)
        except (ValueError, TypeError, AttributeError):
            name = scope.user_id
        clipped = str(text)[:chars]
        selected.append((int(rowid), ContextMessage(name, clipped, str(mid), scope.user_id)))
        chars -= len(clipped)
    return through, version, tuple(selected)


def _commit_batch(path, root, scope, previous, version, rows, candidates, end):
    evidence = {message.message_id: message for _, message in rows}
    with closing(sqlite3.connect(path)) as connection:
        _ensure_schema(connection)
        connection.execute('BEGIN IMMEDIATE')
        existing = connection.execute(
            'SELECT through_message_id,version FROM group_fact_progress WHERE group_id=? AND user_id=?',
            (scope.group_id, scope.user_id),
        ).fetchone()
        if existing is not None and tuple(existing) != (previous, version):
            return False
        actual = tuple(int(row[0]) for row in connection.execute(
            "SELECT rowid FROM chat_messages WHERE group_id=? AND user_id=? AND rowid>? AND rowid<=? "
            "AND trim(plaintext)<>'' AND substr(ltrim(plaintext),1,1)<>'/' ORDER BY rowid",
            (scope.group_id, scope.user_id, previous, end),
        ))
        if actual != tuple(rowid for rowid, _ in rows):
            return False
        now = _now()
        nickname = rows[-1][1].nickname if rows else scope.user_id
        connection.execute(
            "INSERT OR IGNORE INTO member_memories(group_id,user_id,nickname,aliases_json,traits_json,updated_at) "
            "VALUES(?,?,?,'[]','[]',?)", (scope.group_id, scope.user_id, nickname, now),
        )
        for candidate in candidates:
            uid = str(candidate.get('user_id') or '')
            trait = str(candidate.get('trait') or '').strip()
            mid = str(candidate.get('evidence_message_id') or '')
            quote = str(candidate.get('quote') or '').strip()
            source = evidence.get(mid)
            if (uid != scope.user_id or source is None or not quote or quote not in source.text
                or not 2 <= len(trait) <= 80 or SENSITIVE_RE.search(trait) or SENSITIVE_RE.search(quote)
                or contains_secret(trait) or contains_secret(quote)):
                continue
            _append_fact(connection, scope.group_id, uid, trait, mid, now)
        facts = [MemoryTrait(text, mid, created, int(fid)) for fid,text,mid,created in connection.execute(
            "SELECT id,trait,evidence_message_id,created_at FROM member_memory_facts "
            "WHERE group_id=? AND user_id=? AND status='active' ORDER BY id DESC LIMIT ?",
            (scope.group_id, scope.user_id, LEGACY_VIEW_LIMIT),
        )][::-1]
        connection.execute('UPDATE member_memories SET traits_json=?,updated_at=? WHERE group_id=? AND user_id=?',
            (json.dumps([asdict(fact) for fact in facts],ensure_ascii=False),now,scope.group_id,scope.user_id))
        connection.execute(
            "INSERT INTO group_fact_progress(group_id,user_id,through_message_id,version,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(group_id,user_id) DO UPDATE SET through_message_id=excluded.through_message_id,"
            "version=excluded.version,updated_at=excluded.updated_at",
            (scope.group_id,scope.user_id,end,version+1,now),
        )
        connection.commit()
    _write_mirror(path,root,scope.group_id,scope.user_id)
    return True


async def _refresh_bounded(path, root, scope, allowed) -> bool:
    await refresh_member_summary(path,root,group_id=scope.group_id,user_id=scope.user_id,
        strict=True,allowed=lambda: allowed(scope.group_id),max_batches=1)
    pending = await asyncio.to_thread(pending_summary_batch,path,group_id=scope.group_id,user_id=scope.user_id)
    return pending is not None


async def process_member_job(job, *, path: Path, root: Path, allowed: Callable[[int],bool],
                             summary_enabled: bool, batch_messages: int=20, batch_chars: int=12_000):
    # Import lazily: the member matcher is loaded before the private-memory plugin.
    from plugins.private_memory.models import MemoryJobContinuation
    scope = job.scope
    if scope.conversation_kind != 'group' or scope.group_id is None or not allowed(scope.group_id):
        return False
    previous,version,rows = await asyncio.to_thread(_read_batch,path,scope,
        job.input_from_id or job.input_through_id,job.input_through_id,batch_messages,batch_chars)
    if previous >= job.input_through_id:
        # A retry after committed facts must still retry a failed summary.
        if summary_enabled:
            pending = await _refresh_bounded(path,root,scope,allowed)
            if not allowed(scope.group_id):
                return False
            if pending:
                return MemoryJobContinuation.MORE
        return True
    candidates = await extract_memory_candidates([message for _,message in rows],strict=True) if rows else []
    if not allowed(scope.group_id):
        return False
    end = rows[-1][0] if rows else job.input_through_id
    if not await asyncio.to_thread(_commit_batch,path,root,scope,previous,version,rows,candidates,end):
        return False
    pending_summary = False
    if summary_enabled and allowed(scope.group_id):
        pending_summary = await _refresh_bounded(path,root,scope,allowed)
    if not allowed(scope.group_id):
        return False
    return MemoryJobContinuation.MORE if end < job.input_through_id or pending_summary else True
