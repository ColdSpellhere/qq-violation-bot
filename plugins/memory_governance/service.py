from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from plugins.member_memory.store import (
    _ensure_schema as _ensure_member_schema,
    _write_mirror,
)
from plugins.private_memory.schema import PRIVATE_MEMORY_SCHEMA_VERSION, schema_version
from plugins.private_memory.store import _checkpoint_truncate

from .commands import MEMORY_HELP_TEXT, MemoryCommand, MemoryScope


PENDING_TTL = timedelta(minutes=10)


@dataclass(frozen=True)
class PreviewResult:
    token: str
    preview_text: str
    expires_at: str
    operation_id: int

    @property
    def preview(self) -> str:
        return self.preview_text


@dataclass(frozen=True)
class CommitResult:
    success: bool
    message: str
    operation_id: int | None = None
    already_consumed: bool = False
    physical_cleanup_complete: bool | None = None
    mirror_refresh_complete: bool | None = None


@dataclass(frozen=True)
class CancelResult:
    cancelled: bool
    message: str
    operation_id: int | None = None
    already_consumed: bool = False


@dataclass(frozen=True)
class ViewResult:
    text: str


def _utc_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _positive_actor(actor: str) -> str:
    value = str(actor)
    if not value.isascii() or not value.isdigit() or int(value) <= 0:
        raise ValueError("actor must be a positive ASCII decimal string")
    return str(int(value))


class MemoryGovernanceService:
    def __init__(
        self,
        path: Path,
        *,
        private_allowed_user_ids: Iterable[str] = (),
        persona_id: str = "radish-cat",
        member_memory_root: Path | None = None,
    ):
        self.path = Path(path)
        if schema_version(self.path) != PRIVATE_MEMORY_SCHEMA_VERSION:
            raise RuntimeError("private memory schema is not migrated")
        self.private_allowed_user_ids = frozenset(
            self._private_id(value) for value in private_allowed_user_ids
        )
        self.persona_id = str(persona_id)
        if not self.persona_id:
            raise ValueError("persona_id must not be empty")
        self.member_memory_root = Path(member_memory_root) if member_memory_root else None
        with closing(self._connect()) as connection:
            _ensure_member_schema(connection)
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _private_id(value: object) -> str:
        text = str(value)
        if not text.isascii() or not text.isdigit() or int(text) <= 0:
            raise ValueError("private user IDs must be positive ASCII decimal strings")
        return str(int(text))

    def _validate_scope(self, scope: MemoryScope | None) -> MemoryScope:
        if not isinstance(scope, MemoryScope):
            raise ValueError("command requires a target scope")
        user_id = self._private_id(scope.user_id)
        if scope.kind == "private":
            if scope.group_id is not None:
                raise ValueError("private scope must not contain group_id")
            if user_id not in self.private_allowed_user_ids:
                raise ValueError("private target is not in the existing allowlist")
            return MemoryScope("private", user_id)
        if scope.kind != "group":
            raise ValueError("scope kind must be group or private")
        if (
            isinstance(scope.group_id, bool)
            or not isinstance(scope.group_id, int)
            or scope.group_id <= 0
        ):
            raise ValueError("group scope requires a positive group_id")
        return MemoryScope("group", user_id, scope.group_id)

    def preview(
        self, command: MemoryCommand, *, actor: str, now: datetime
    ) -> PreviewResult:
        actor = _positive_actor(actor)
        created_at = _utc_text(now)
        expires_at = _utc_text(now + PENDING_TTL)
        payload, preview_text, target = self._prepare(command)
        token = secrets.token_urlsafe(32)
        command_json = _canonical(payload)
        envelope = {
            "binding": hashlib.sha256(
                f"{token}\n{actor}\n{command_json}".encode("utf-8")
            ).hexdigest(),
            "command": payload,
        }
        payload_json = _canonical(envelope)
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    INSERT INTO memory_pending_operations(
                        confirmation_token_hash,operator_user_id,operation_type,target_kind,
                        target_group_id,target_user_id,target_memory_id,payload_json,
                        preview_text,expires_at,consumed_at,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?)
                    """,
                    (
                        _token_hash(token), actor, command.action, target[0], target[1],
                        target[2], target[3], payload_json, preview_text, expires_at, created_at,
                    ),
                )
                operation_id = int(cursor.lastrowid)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return PreviewResult(token, preview_text, expires_at, operation_id)

    def confirm(
        self, token: str, *, actor: str, reason: str, now: datetime
    ) -> CommitResult:
        actor = _positive_actor(actor)
        token = str(token)
        reason = " ".join(str(reason).split())
        if not reason:
            return CommitResult(False, "确认原因不能为空。")
        if len(reason) > 500:
            return CommitResult(False, "确认原因不能超过 500 个字符。")
        now_text = _utc_text(now)
        committed_payload: dict[str, Any] | None = None
        operation_id: int | None = None
        with closing(self._connect()) as connection:
            try:
                connection.execute("PRAGMA secure_delete=ON")
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM memory_pending_operations WHERE confirmation_token_hash=?",
                    (_token_hash(token),),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return CommitResult(False, "操作码无效。")
                operation_id = int(row["id"])
                if str(row["operator_user_id"]) != actor:
                    connection.rollback()
                    return CommitResult(False, "操作码不属于当前操作人。", operation_id)
                if row["consumed_at"] is not None:
                    connection.rollback()
                    return CommitResult(
                        False, "该操作码已经使用。", operation_id, already_consumed=True
                    )
                if now.astimezone(timezone.utc) >= _parse_utc(str(row["expires_at"])):
                    self._consume_failed(
                        connection, row, actor=actor, reason=reason,
                        now_text=now_text, error_code="expired",
                    )
                    connection.commit()
                    return CommitResult(False, "操作码已过期。", operation_id)
                payload = self._verified_payload(row, token, actor)
                if payload is None:
                    self._consume_failed(
                        connection, row, actor=actor, reason=reason,
                        now_text=now_text, error_code="payload_invalid",
                    )
                    connection.commit()
                    return CommitResult(False, "操作内容校验失败。", operation_id)
                connection.execute("SAVEPOINT governance_apply")
                try:
                    before, after = self._apply(connection, payload, operation_id, now_text)
                    self._insert_audit(
                        connection,
                        operation_id=operation_id,
                        actor=actor,
                        row=row,
                        before=before,
                        after=after,
                        reason=reason,
                        result="success",
                        error_code="",
                        now_text=now_text,
                    )
                    connection.execute("RELEASE governance_apply")
                except (ValueError, sqlite3.Error) as exc:
                    connection.execute("ROLLBACK TO governance_apply")
                    connection.execute("RELEASE governance_apply")
                    error_code = "conflict" if isinstance(exc, ValueError) else "db_error"
                    self._consume_failed(
                        connection, row, actor=actor, reason=reason,
                        now_text=now_text, error_code=error_code,
                    )
                    connection.commit()
                    return CommitResult(
                        False,
                        "记忆治理操作因状态变化而失败。"
                        if error_code == "conflict"
                        else "记忆治理操作失败。",
                        operation_id,
                    )
                cursor = connection.execute(
                    "UPDATE memory_pending_operations SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
                    (now_text, operation_id),
                )
                if cursor.rowcount != 1:
                    raise sqlite3.IntegrityError("pending operation was consumed concurrently")
                connection.commit()
                committed_payload = payload
            except (ValueError, sqlite3.Error):
                connection.rollback()
                return CommitResult(False, "记忆治理操作失败。", operation_id)

        assert committed_payload is not None and operation_id is not None
        physical_cleanup_complete: bool | None = None
        mirror_refresh_complete: bool | None = None
        message = "记忆治理操作已提交。"
        if committed_payload.get("action") == "clear_private":
            physical_cleanup_complete = _checkpoint_truncate(self.path)
            if not physical_cleanup_complete:
                message = "逻辑变更已提交，但物理清理未完成，需要重试 WAL checkpoint。"
                self._mark_physical_cleanup_pending(operation_id)
        mirror_target = self._group_mirror_target(committed_payload)
        if mirror_target is not None and self.member_memory_root is not None:
            try:
                mirror_refresh_complete = _write_mirror(
                    self.path, self.member_memory_root, mirror_target[0], mirror_target[1]
                )
            except Exception:
                mirror_refresh_complete = False
            if not mirror_refresh_complete:
                message = "数据库已提交，但镜像刷新失败，需要重试。"
                self._mark_mirror_refresh_failed(operation_id)
        return CommitResult(
            True, message, operation_id,
            physical_cleanup_complete=physical_cleanup_complete,
            mirror_refresh_complete=mirror_refresh_complete,
        )

    def _mark_physical_cleanup_pending(self, operation_id: int) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE memory_governance_audit SET error_code='physical_cleanup_pending' "
                "WHERE operation_id=? AND result='success'",
                (operation_id,),
            )
            connection.commit()

    def cancel(self, token: str, *, actor: str, now: datetime) -> CancelResult:
        actor = _positive_actor(actor)
        token = str(token)
        now_text = _utc_text(now)
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM memory_pending_operations WHERE confirmation_token_hash=?",
                    (_token_hash(token),),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return CancelResult(False, "操作码无效。")
                operation_id = int(row["id"])
                if str(row["operator_user_id"]) != actor:
                    connection.rollback()
                    return CancelResult(False, "操作码不属于当前操作人。", operation_id)
                if row["consumed_at"] is not None:
                    connection.rollback()
                    return CancelResult(
                        False, "该操作码已经使用。", operation_id, already_consumed=True
                    )
                if now.astimezone(timezone.utc) >= _parse_utc(str(row["expires_at"])):
                    self._consume_failed(
                        connection,
                        row,
                        actor=actor,
                        reason="cancel_after_expiry",
                        now_text=now_text,
                        error_code="expired",
                    )
                    connection.commit()
                    return CancelResult(False, "操作码已过期。", operation_id)
                payload = self._verified_payload(row, token, actor)
                if payload is None:
                    connection.rollback()
                    return CancelResult(False, "操作内容校验失败。", operation_id)
                self._insert_audit(
                    connection,
                    operation_id=operation_id,
                    actor=actor,
                    row=row,
                    before=payload.get("before", {}),
                    after=payload.get("before", {}),
                    reason="operator_cancelled",
                    result="cancelled",
                    error_code="",
                    now_text=now_text,
                )
                connection.execute(
                    "UPDATE memory_pending_operations SET consumed_at=? WHERE id=?",
                    (now_text, operation_id),
                )
                connection.commit()
                return CancelResult(True, "已取消记忆治理操作。", operation_id)
            except (ValueError, sqlite3.Error):
                connection.rollback()
                return CancelResult(False, "取消记忆治理操作失败。")

    def view(self, command: MemoryCommand, *, actor: str) -> ViewResult:
        _positive_actor(actor)
        if command.action == "help":
            return ViewResult(MEMORY_HELP_TEXT)
        if command.action == "status":
            with closing(self._connect()) as connection:
                pending = int(connection.execute(
                    "SELECT count(*) FROM memory_pending_operations WHERE consumed_at IS NULL"
                ).fetchone()[0])
                audits = int(connection.execute(
                    "SELECT count(*) FROM memory_governance_audit"
                ).fetchone()[0])
            return ViewResult(f"待确认操作：{pending}\n治理审计记录：{audits}")
        scope = self._validate_scope(command.scope)
        if command.action == "view_relation":
            return self._view_relation(scope)
        if command.action != "view_facts":
            raise ValueError("command is not a supported view")
        with closing(self._connect()) as connection:
            if scope.kind == "private":
                rows = connection.execute(
                    "SELECT id,fact_text,status,trust_level,created_at FROM private_memory_facts WHERE user_id=? ORDER BY id",
                    (scope.user_id,),
                ).fetchall()
                prefix = "P"
            else:
                rows = connection.execute(
                    "SELECT id,trait,status,trust_level,created_at FROM member_memory_facts WHERE group_id=? AND user_id=? ORDER BY id",
                    (scope.group_id, scope.user_id),
                ).fetchall()
                prefix = "G"
        if not rows:
            return ViewResult("暂无记忆事实。")
        return ViewResult("\n".join(
            f"{prefix}-{row[0]} [{row[2]}/{row[3]}] {row[1]}" for row in rows
        ))

    def _view_relation(self, scope: MemoryScope) -> ViewResult:
        with closing(self._connect()) as connection:
            if scope.kind == "private":
                row = connection.execute(
                    "SELECT state_text,open_topics_json,preferred_address,communication_style,version FROM relationship_states WHERE conversation_kind='private' AND group_id IS NULL AND user_id=? AND persona_id=?",
                    (scope.user_id, self.persona_id),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT state_text,open_topics_json,preferred_address,communication_style,version FROM relationship_states WHERE conversation_kind='group' AND group_id=? AND user_id=? AND persona_id=?",
                    (scope.group_id, scope.user_id, self.persona_id),
                ).fetchone()
        if row is None:
            return ViewResult("暂无关系状态。")
        return ViewResult(
            f"关系状态（v{row['version']}）：{row['state_text'] or '暂无'}\n"
            f"未完话题：{row['open_topics_json']}\n称呼：{row['preferred_address'] or '无'}\n"
            f"交流方式：{row['communication_style'] or '无'}"
        )

    def _prepare(
        self, command: MemoryCommand
    ) -> tuple[dict[str, Any], str, tuple[str, int | None, str, int | None]]:
        if not isinstance(command, MemoryCommand) or not command.is_write:
            raise ValueError("preview requires a write command")
        base: dict[str, Any] = {
            "action": command.action,
            "content": command.content,
            "fact_kind": command.fact_kind,
            "memory_id": command.memory_id,
            "scope": asdict(command.scope) if command.scope else None,
        }
        if command.action in {"add_fact", "modify_fact"}:
            if not command.content.strip() or len(command.content) > 80:
                raise ValueError("fact content must contain 1 to 80 characters")
        if command.action == "update_relation":
            if not command.content.strip() or len(command.content) > 600:
                raise ValueError("relationship content must contain 1 to 600 characters")
        with closing(self._connect()) as connection:
            if command.action in {"add_fact", "update_relation", "clear_private"}:
                scope = self._validate_scope(command.scope)
                base["scope"] = asdict(scope)
            if command.action == "add_fact":
                target = (scope.kind, scope.group_id, scope.user_id, None)
                return base, self._bounded_preview(
                    f"目标：{self._scope_label(scope)}\n操作：添加事实\n新增：{command.content}"
                ), target
            if command.action == "update_relation":
                before = self._relation_snapshot(connection, scope)
                base["before"] = before
                target = ("relationship", scope.group_id, scope.user_id, None)
                return base, self._bounded_preview(
                    f"目标：{self._scope_label(scope)}\n操作：更新关系\n"
                    f"原状态：{(before or {}).get('state_text') or '暂无'}\n"
                    f"新状态：{command.content}"
                ), target
            if command.action == "clear_private":
                snapshot = self._clear_snapshot(connection, scope.user_id)
                counts = self._clear_preview_counts(snapshot)
                base["before"] = snapshot
                target = ("private", None, scope.user_id, None)
                return base, self._bounded_preview(
                    f"目标：{self._scope_label(scope)}\n操作：清空私聊短期层\n影响："
                    f"原文 {counts['messages']} 条、摘要 {counts['summaries']} 条、"
                    f"未完话题 {counts['topics']} 组、摘要任务 {counts['jobs']} 个。"
                ), target
            fact_kind = command.fact_kind
            memory_id = command.memory_id
            if fact_kind not in {"group", "private"} or not isinstance(memory_id, int) or memory_id <= 0:
                raise ValueError("fact command requires a scoped positive memory id")
            before = self._fact_snapshot(connection, fact_kind, memory_id)
            if before is None or before["status"] != "active":
                raise ValueError("active fact not found")
            if (
                fact_kind == "private"
                and str(before["user_id"]) not in self.private_allowed_user_ids
            ):
                raise ValueError("private target is not in the existing allowlist")
            base["before"] = before
            target = ("fact", before.get("group_id"), before["user_id"], memory_id)
            label = "修改" if command.action == "modify_fact" else "删除"
            scope_label = self._fact_scope_label(fact_kind, before)
            preview = (
                f"目标：{scope_label}\n操作：{label} {fact_kind[0].upper()}-{memory_id}\n"
                f"原内容：{before['text']}"
            )
            if command.action == "modify_fact":
                preview += f"\n新内容：{command.content}"
            return base, self._bounded_preview(preview), target

    @staticmethod
    def _scope_label(scope: MemoryScope) -> str:
        if scope.kind == "private":
            return f"私聊 / QQ {scope.user_id}"
        return f"群聊 {scope.group_id} / QQ {scope.user_id}"

    @classmethod
    def _fact_scope_label(cls, kind: str, before: dict[str, Any]) -> str:
        return cls._scope_label(MemoryScope(
            kind, str(before["user_id"]),
            int(before["group_id"]) if kind == "group" else None,
        ))

    @staticmethod
    def _bounded_preview(value: str) -> str:
        if len(value) > 1600:
            raise ValueError("governance preview exceeds safe display length")
        return value

    @staticmethod
    def _verified_payload(row: sqlite3.Row, token: str, actor: str) -> dict[str, Any] | None:
        try:
            envelope = json.loads(str(row["payload_json"]))
            payload = envelope["command"]
            canonical = _canonical(payload)
            expected = hashlib.sha256(f"{token}\n{actor}\n{canonical}".encode("utf-8")).hexdigest()
            if not secrets.compare_digest(str(envelope["binding"]), expected):
                return None
            if _canonical(envelope) != str(row["payload_json"]):
                return None
            if str(payload.get("action")) != str(row["operation_type"]):
                return None
            return payload
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _apply(
        self, connection: sqlite3.Connection, payload: dict[str, Any], operation_id: int, now: str
    ) -> tuple[Any, Any]:
        action = str(payload["action"])
        if action == "add_fact":
            return self._apply_add(connection, payload, operation_id, now)
        if action in {"modify_fact", "delete_fact"}:
            return self._apply_fact_change(connection, payload, operation_id, now)
        if action == "update_relation":
            return self._apply_relation(connection, payload, operation_id, now)
        if action == "clear_private":
            return self._apply_clear(connection, payload, operation_id, now)
        raise ValueError("unsupported pending operation")

    def _apply_add(self, connection, payload, operation_id, now):
        scope = self._validate_scope(self._scope_from_payload(payload))
        text = str(payload["content"])
        source = f"governance:{operation_id}"
        if scope.kind == "private":
            cursor = connection.execute(
                """
                INSERT INTO private_memory_facts(
                    user_id,fact_text,normalized_text,source_message_id,source_quote,
                    trust_level,status,supersedes_id,version,created_at,updated_at,deleted_at
                ) VALUES(?,?,?,?,?,'admin_confirmed','active',NULL,1,?,?,NULL)
                """,
                (scope.user_id, text, text.casefold(), source, text[:120], now, now),
            )
            fact_id = int(cursor.lastrowid)
        else:
            connection.execute(
                """
                INSERT INTO member_memories(
                    group_id,user_id,nickname,aliases_json,traits_json,updated_at
                ) VALUES(?,?,?,'[]','[]',?)
                ON CONFLICT(group_id,user_id) DO NOTHING
                """,
                (scope.group_id, scope.user_id, scope.user_id, now),
            )
            cursor = connection.execute(
                """
                INSERT INTO member_memory_facts(
                    group_id,user_id,trait,evidence_message_id,created_at,trust_level,
                    status,supersedes_id,updated_at,version,deleted_at
                ) VALUES(?,?,?,?,?,'admin_confirmed','active',NULL,?,1,NULL)
                """,
                (scope.group_id, scope.user_id, text, source, now, now),
            )
            fact_id = int(cursor.lastrowid)
        return {}, {"id": fact_id, "text": text, "status": "active", "trust": "admin_confirmed"}

    def _apply_fact_change(self, connection, payload, operation_id, now):
        kind, memory_id = str(payload["fact_kind"]), int(payload["memory_id"])
        current = self._fact_snapshot(connection, kind, memory_id)
        if current != payload.get("before") or current is None or current["status"] != "active":
            raise ValueError("fact changed after preview")
        if kind == "private" and str(current["user_id"]) not in self.private_allowed_user_ids:
            raise ValueError("private target is not in the existing allowlist")
        table = "private_memory_facts" if kind == "private" else "member_memory_facts"
        if payload["action"] == "delete_fact":
            cursor = connection.execute(
                f"UPDATE {table} SET status='deleted',deleted_at=?,updated_at=?,version=version+1 WHERE id=? AND status='active'",  # noqa: S608
                (now, now, memory_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("fact delete conflict")
            if kind == "group":
                self._invalidate_group_summary(
                    connection, int(current["group_id"]), str(current["user_id"]), now
                )
            return current, {**current, "status": "deleted"}
        text = str(payload["content"])
        connection.execute(
            f"UPDATE {table} SET status='superseded',updated_at=?,version=version+1 WHERE id=? AND status='active'",  # noqa: S608
            (now, memory_id),
        )
        source = f"governance:{operation_id}"
        new_version = int(current["version"]) + 1
        if kind == "private":
            cursor = connection.execute(
                """
                INSERT INTO private_memory_facts(
                    user_id,fact_text,normalized_text,source_message_id,source_quote,
                    trust_level,status,supersedes_id,version,created_at,updated_at,deleted_at
                ) VALUES(?,?,?,?,?,'admin_confirmed','active',?,?,?,?,NULL)
                """,
                (current["user_id"], text, text.casefold(), source, text[:120], memory_id, new_version, now, now),
            )
        else:
            cursor = connection.execute(
                """
                INSERT INTO member_memory_facts(
                    group_id,user_id,trait,evidence_message_id,created_at,trust_level,
                    status,supersedes_id,updated_at,version,deleted_at
                ) VALUES(?,?,?,?,?,'admin_confirmed','active',?,?,?,NULL)
                """,
                (current["group_id"], current["user_id"], text, source, now, memory_id, now, new_version),
            )
            self._invalidate_group_summary(
                connection, int(current["group_id"]), str(current["user_id"]), now
            )
        return current, {
            "id": int(cursor.lastrowid), "text": text, "status": "active",
            "supersedes_id": memory_id, "version": new_version,
        }

    def _apply_relation(self, connection, payload, operation_id, now):
        scope = self._validate_scope(self._scope_from_payload(payload))
        current = self._relation_snapshot(connection, scope)
        if current != payload.get("before"):
            raise ValueError("relationship changed after preview")
        source = f"governance:{operation_id}"
        text = str(payload["content"])
        if current is None:
            connection.execute(
                """
                INSERT INTO relationship_states(
                    conversation_kind,group_id,user_id,persona_id,state_text,open_topics_json,
                    preferred_address,communication_style,source_message_id,source_watermark,
                    version,created_at,updated_at
                ) VALUES(?,?,?,?,?,'[]','','',?,0,1,?,?)
                """,
                (scope.kind, scope.group_id, scope.user_id, self.persona_id, text, source, now, now),
            )
            after = {"state_text": text, "source_watermark": 0, "version": 1}
        else:
            where, values = self._relation_where(scope)
            cursor = connection.execute(
                f"UPDATE relationship_states SET state_text=?,source_message_id=?,version=version+1,updated_at=? WHERE {where} AND version=? AND source_watermark=?",  # noqa: S608
                (text, source, now, *values, current["version"], current["source_watermark"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("relationship update conflict")
            after = {**current, "state_text": text, "version": int(current["version"]) + 1}
        return current or {}, after

    def _apply_clear(self, connection, payload, operation_id, now):
        scope = self._validate_scope(self._scope_from_payload(payload))
        before = self._clear_snapshot(connection, scope.user_id)
        if before != payload.get("before"):
            raise ValueError("private layers changed after preview")
        maximum = int(connection.execute(
            "SELECT COALESCE(MAX(id),0) FROM private_chat_messages WHERE user_id=?", (scope.user_id,)
        ).fetchone()[0])
        connection.execute(
            "UPDATE private_chat_messages "
            "SET text='',image_descriptions_json='[]',purged_at=? "
            "WHERE user_id=? AND purged_at IS NULL",
            (now, scope.user_id),
        )
        connection.execute(
            """
            INSERT INTO private_conversation_summaries(
                user_id,summary_text,source_start_id,source_end_id,summarized_through_id,
                version,created_at,updated_at
            ) VALUES(?,'',0,0,?,1,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                summary_text='',source_start_id=0,source_end_id=0,
                summarized_through_id=excluded.summarized_through_id,
                version=private_conversation_summaries.version+1,
                updated_at=excluded.updated_at
            """,
            (scope.user_id, maximum, now, now),
        )
        connection.execute(
            "UPDATE relationship_states SET open_topics_json='[]',source_message_id=?,version=version+1,updated_at=? WHERE conversation_kind='private' AND group_id IS NULL AND user_id=? AND open_topics_json<>'[]'",
            (f"governance:{operation_id}", now, scope.user_id),
        )
        connection.execute(
            "UPDATE memory_jobs SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE conversation_kind='private' AND user_id=? AND job_type='private_summary' AND status IN ('pending','running')",
            (now, scope.user_id),
        )
        return before, self._clear_snapshot(connection, scope.user_id)

    @staticmethod
    def _invalidate_group_summary(
        connection: sqlite3.Connection, group_id: int, user_id: str, now: str
    ) -> None:
        connection.execute(
            """
            UPDATE member_memory_summaries
            SET summary_text='',through_fact_id=0,updated_at=?
            WHERE group_id=? AND user_id=?
            """,
            (now, group_id, user_id),
        )

    @staticmethod
    def _insert_audit(
        connection: sqlite3.Connection,
        *, operation_id: int, actor: str, row: sqlite3.Row, before: Any,
        after: Any, reason: str, result: str, error_code: str, now_text: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO memory_governance_audit(
                operation_id,operator_user_id,target_kind,target_group_id,target_user_id,
                target_memory_id,operation_type,before_hash,after_hash,reason,result,
                error_code,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                operation_id, actor, row["target_kind"], row["target_group_id"],
                row["target_user_id"], row["target_memory_id"], row["operation_type"],
                _digest(before), _digest(after), reason, result, error_code, now_text,
            ),
        )

    def _consume_failed(self, connection, row, *, actor, reason, now_text, error_code):
        self._insert_audit(
            connection, operation_id=int(row["id"]), actor=actor, row=row,
            before={}, after={}, reason=reason, result="failed",
            error_code=error_code, now_text=now_text,
        )
        connection.execute(
            "UPDATE memory_pending_operations SET consumed_at=? WHERE id=?",
            (now_text, int(row["id"])),
        )

    @staticmethod
    def _group_mirror_target(payload: dict[str, Any]) -> tuple[int, str] | None:
        if payload.get("action") == "add_fact":
            raw = payload.get("scope")
        elif payload.get("action") in {"modify_fact", "delete_fact"}:
            raw = payload.get("before")
        else:
            return None
        if not isinstance(raw, dict) or raw.get("group_id") is None:
            return None
        try:
            return int(raw["group_id"]), str(raw["user_id"])
        except (KeyError, TypeError, ValueError):
            return None

    def _mark_mirror_refresh_failed(self, operation_id: int) -> None:
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE memory_governance_audit SET error_code='mirror_refresh_failed' "
                    "WHERE operation_id=? AND result='success'",
                    (operation_id,),
                )
                connection.commit()
        except sqlite3.Error:
            return

    @staticmethod
    def _scope_from_payload(payload: dict[str, Any]) -> MemoryScope:
        raw = payload.get("scope")
        if not isinstance(raw, dict):
            raise ValueError("pending scope is invalid")
        return MemoryScope(str(raw["kind"]), str(raw["user_id"]), raw.get("group_id"))

    @staticmethod
    def _fact_snapshot(connection, kind: str, memory_id: int) -> dict[str, Any] | None:
        if kind == "private":
            row = connection.execute(
                "SELECT id,user_id,fact_text,status,trust_level,version,deleted_at FROM private_memory_facts WHERE id=?",
                (memory_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "id": int(row["id"]), "user_id": str(row["user_id"]),
                "text": str(row["fact_text"]), "status": str(row["status"]),
                "trust_level": str(row["trust_level"]), "version": int(row["version"]),
                "deleted_at": row["deleted_at"],
            }
        row = connection.execute(
            "SELECT id,group_id,user_id,trait,status,trust_level,version,deleted_at FROM member_memory_facts WHERE id=?",
            (memory_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": int(row["id"]), "group_id": int(row["group_id"]),
            "user_id": str(row["user_id"]), "text": str(row["trait"]),
            "status": str(row["status"]), "trust_level": str(row["trust_level"]),
            "version": int(row["version"]), "deleted_at": row["deleted_at"],
        }

    def _relation_snapshot(self, connection, scope: MemoryScope) -> dict[str, Any] | None:
        where, values = self._relation_where(scope)
        row = connection.execute(
            f"SELECT state_text,open_topics_json,preferred_address,communication_style,source_watermark,version FROM relationship_states WHERE {where}",  # noqa: S608
            values,
        ).fetchone()
        if row is None:
            return None
        return {
            "state_text": str(row["state_text"]),
            "open_topics_json": str(row["open_topics_json"]),
            "preferred_address": str(row["preferred_address"]),
            "communication_style": str(row["communication_style"]),
            "source_watermark": int(row["source_watermark"]),
            "version": int(row["version"]),
        }

    def _relation_where(self, scope: MemoryScope) -> tuple[str, tuple[Any, ...]]:
        if scope.kind == "private":
            return (
                "conversation_kind='private' AND group_id IS NULL AND user_id=? AND persona_id=?",
                (scope.user_id, self.persona_id),
            )
        return (
            "conversation_kind='group' AND group_id=? AND user_id=? AND persona_id=?",
            (scope.group_id, scope.user_id, self.persona_id),
        )

    @staticmethod
    def _clear_snapshot(connection, user_id: str) -> dict[str, Any]:
        summary = connection.execute(
            "SELECT version,summarized_through_id,summary_text FROM private_conversation_summaries WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return {
            "message_ids": [
                int(row[0]) for row in connection.execute(
                    "SELECT id FROM private_chat_messages WHERE user_id=? AND purged_at IS NULL ORDER BY id",
                    (user_id,),
                )
            ],
            "summary": (
                {
                    "version": int(summary["version"]),
                    "through": int(summary["summarized_through_id"]),
                    "has_text": bool(str(summary["summary_text"])),
                }
                if summary is not None
                else None
            ),
            "topic_rows": [
                [int(row[0]), int(row[1])]
                for row in connection.execute(
                    "SELECT id,version FROM relationship_states WHERE conversation_kind='private' AND group_id IS NULL AND user_id=? AND open_topics_json<>'[]' ORDER BY id",
                    (user_id,),
                )
            ],
            "summary_jobs": [
                [int(row[0]), str(row[1]), int(row[2])]
                for row in connection.execute(
                    "SELECT id,status,claim_version FROM memory_jobs WHERE conversation_kind='private' AND user_id=? AND job_type='private_summary' AND status IN ('pending','running') ORDER BY id",
                    (user_id,),
                )
            ],
        }

    @staticmethod
    def _clear_preview_counts(snapshot: dict[str, Any]) -> dict[str, int]:
        return {
            "messages": len(snapshot["message_ids"]),
            "summaries": int(bool(snapshot["summary"] and snapshot["summary"]["has_text"])),
            "topics": len(snapshot["topic_rows"]),
            "jobs": len(snapshot["summary_jobs"]),
        }


__all__ = [
    "CancelResult",
    "CommitResult",
    "MemoryGovernanceService",
    "PreviewResult",
    "ViewResult",
]
