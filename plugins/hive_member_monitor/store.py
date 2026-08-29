from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Mapping, Sequence


SCHEMA = """
CREATE TABLE IF NOT EXISTS hive_monitor_members (
    group_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    nickname TEXT NOT NULL,
    card TEXT NOT NULL,
    qq_name TEXT NOT NULL,
    role TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    missing_count INTEGER NOT NULL DEFAULT 0 CHECK(missing_count >= 0),
    first_seen_at TEXT NOT NULL,
    episode_started_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    left_at TEXT,
    PRIMARY KEY(group_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_hive_monitor_members_active
ON hive_monitor_members(group_id, active, user_id);

CREATE TABLE IF NOT EXISTS hive_monitor_group_state (
    group_id INTEGER PRIMARY KEY,
    initial_export_delivered_at TEXT,
    initial_export_report_group_id INTEGER,
    initial_export_file_name TEXT NOT NULL DEFAULT '',
    initial_export_sha256 TEXT NOT NULL DEFAULT '',
    last_successful_sync_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hive_monitor_departure_outbox (
    event_key TEXT PRIMARY KEY,
    group_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    qq_name TEXT NOT NULL,
    sub_type TEXT NOT NULL,
    operator_id TEXT NOT NULL DEFAULT '',
    event_time INTEGER NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'delivered')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    last_error TEXT NOT NULL DEFAULT '',
    lease_token TEXT NOT NULL DEFAULT '',
    lease_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_hive_monitor_departure_pending
ON hive_monitor_departure_outbox(status, created_at, event_key);

CREATE TABLE IF NOT EXISTS hive_monitor_mass_candidates (
    group_id INTEGER PRIMARY KEY,
    baseline_sha256 TEXT NOT NULL,
    candidate_sha256 TEXT NOT NULL,
    member_count INTEGER NOT NULL CHECK(member_count > 0),
    confirmations INTEGER NOT NULL CHECK(confirmations > 0),
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class MemberSnapshot:
    user_id: str
    qq_name: str
    nickname: str = ""
    card: str = ""
    role: str = "member"
    group_id: int | None = None


# A descriptive alias for callers that prefer to emphasize normalization.
NormalizedMember = MemberSnapshot


@dataclass(frozen=True)
class SnapshotDelta:
    joined: tuple[MemberSnapshot, ...] = ()
    departed: tuple[MemberSnapshot, ...] = ()


@dataclass(frozen=True)
class DepartureEvent:
    event_key: str
    group_id: int
    user_id: str
    qq_name: str
    sub_type: str
    operator_id: str
    event_time: int
    source: str
    status: str
    attempt_count: int
    last_error: str
    lease_token: str
    lease_until: str


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _positive_decimal(value: object) -> str | None:
    text = _clean_text(value)
    if not text.isdecimal():
        return None
    try:
        number = int(text)
    except ValueError:
        return None
    return str(number) if number > 0 else None


def _member_from_mapping(item: Mapping[str, object]) -> MemberSnapshot | None:
    user_id = _positive_decimal(item.get("user_id"))
    if user_id is None:
        return None
    nickname = _clean_text(item.get("nickname"))
    card = _clean_text(item.get("card"))
    role = _clean_text(item.get("role")) or "member"
    raw_group_id = _positive_decimal(item.get("group_id"))
    return MemberSnapshot(
        user_id=user_id,
        qq_name=card or nickname or user_id,
        nickname=nickname,
        card=card,
        role=role,
        group_id=int(raw_group_id) if raw_group_id is not None else None,
    )


def _quality(member: MemberSnapshot) -> tuple[int, int, int]:
    """Rank duplicate rows without depending on OneBot response order."""

    return (
        2 if member.card else 1 if member.nickname else 0,
        len(member.card),
        len(member.nickname),
    )


def normalize_members(members: Iterable[Mapping[str, object] | MemberSnapshot]) -> list[MemberSnapshot]:
    """Normalize, deduplicate, and numerically sort OneBot member rows.

    Malformed individual rows are ignored.  The caller decides whether an empty
    normalized result is a valid operation; snapshot replacement rejects it so a
    bad API response can never erase the last known member list.
    """

    if isinstance(members, (str, bytes, bytearray, Mapping)):
        raise TypeError("member payload must be a sequence of member rows")

    by_user_id: dict[str, MemberSnapshot] = {}
    try:
        iterator = iter(members)
    except TypeError as exc:
        raise TypeError("member payload must be iterable") from exc

    for raw in iterator:
        if isinstance(raw, MemberSnapshot):
            user_id = _positive_decimal(raw.user_id)
            if user_id is None:
                continue
            nickname = _clean_text(raw.nickname)
            card = _clean_text(raw.card)
            candidate = MemberSnapshot(
                user_id=user_id,
                qq_name=card or nickname or user_id,
                nickname=nickname,
                card=card,
                role=_clean_text(raw.role) or "member",
                group_id=raw.group_id,
            )
        elif isinstance(raw, Mapping):
            candidate = _member_from_mapping(raw)
            if candidate is None:
                continue
        else:
            continue

        existing = by_user_id.get(candidate.user_id)
        if existing is None or _quality(candidate) > _quality(existing):
            by_user_id[candidate.user_id] = candidate

    return sorted(by_user_id.values(), key=lambda item: int(item.user_id))


def departure_event_key(
    group_id: int,
    user_id: int | str,
    sub_type: str,
    event_time: int,
    *,
    source: str = "OneBot",
) -> str:
    material = "\x1f".join(
        (
            str(int(group_id)),
            str(user_id).strip(),
            str(sub_type).strip(),
            str(int(event_time)),
            str(source).strip(),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _timestamp(value: datetime | str | None = None) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if value is not None:
        text = str(value).strip()
        if text:
            return text
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _row_to_member(row: sqlite3.Row) -> MemberSnapshot:
    return MemberSnapshot(
        group_id=int(row["group_id"]),
        user_id=str(row["user_id"]),
        nickname=str(row["nickname"]),
        card=str(row["card"]),
        qq_name=str(row["qq_name"]),
        role=str(row["role"]),
    )


def _row_to_departure(row: sqlite3.Row) -> DepartureEvent:
    return DepartureEvent(
        event_key=str(row["event_key"]),
        group_id=int(row["group_id"]),
        user_id=str(row["user_id"]),
        qq_name=str(row["qq_name"]),
        sub_type=str(row["sub_type"]),
        operator_id=str(row["operator_id"]),
        event_time=int(row["event_time"]),
        source=str(row["source"]),
        status=str(row["status"]),
        attempt_count=int(row["attempt_count"]),
        last_error=str(row["last_error"]),
        lease_token=str(row["lease_token"]),
        lease_until=str(row["lease_until"] or ""),
    )


class MemberSnapshotStore:
    """Instance-local, atomic member snapshot and departure outbox."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        parent_existed = self.database_path.parent.exists()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        if not parent_existed:
            os.chmod(self.database_path.parent, 0o700)
        with self._connection() as conn:
            conn.executescript(SCHEMA)
            member_columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(hive_monitor_members)"
                ).fetchall()
            }
            if "episode_started_at" not in member_columns:
                conn.execute(
                    "ALTER TABLE hive_monitor_members ADD COLUMN "
                    "episode_started_at TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                "UPDATE hive_monitor_members SET episode_started_at=first_seen_at "
                "WHERE episode_started_at=''"
            )
            columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(hive_monitor_departure_outbox)"
                ).fetchall()
            }
            migrations = {
                "operator_id": "TEXT NOT NULL DEFAULT ''",
                "lease_token": "TEXT NOT NULL DEFAULT ''",
                "lease_until": "TEXT",
            }
            for column, declaration in migrations.items():
                if column not in columns:
                    conn.execute(
                        "ALTER TABLE hive_monitor_departure_outbox "
                        f"ADD COLUMN {column} {declaration}"
                    )
            group_state_columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(hive_monitor_group_state)"
                ).fetchall()
            }
            group_state_migrations = {
                "initial_export_report_group_id": "INTEGER",
                "initial_export_file_name": "TEXT NOT NULL DEFAULT ''",
                "initial_export_sha256": "TEXT NOT NULL DEFAULT ''",
            }
            for column, declaration in group_state_migrations.items():
                if column not in group_state_columns:
                    conn.execute(
                        "ALTER TABLE hive_monitor_group_state "
                        f"ADD COLUMN {column} {declaration}"
                    )
        os.chmod(self.database_path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _validated_group_id(group_id: int) -> int:
        value = int(group_id)
        if value <= 0:
            raise ValueError("group_id must be positive")
        return value

    def list_members(self, group_id: int, *, active_only: bool = True) -> list[MemberSnapshot]:
        group = self._validated_group_id(group_id)
        where = " AND active=1" if active_only else ""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT group_id,user_id,nickname,card,qq_name,role "
                f"FROM hive_monitor_members WHERE group_id=?{where} "
                "ORDER BY length(user_id),user_id",
                (group,),
            ).fetchall()
        return [_row_to_member(row) for row in rows]

    def get_member(
        self,
        group_id: int,
        user_id: int | str,
        *,
        include_inactive: bool = True,
    ) -> MemberSnapshot | None:
        group = self._validated_group_id(group_id)
        user = _positive_decimal(user_id)
        if user is None:
            return None
        active_clause = "" if include_inactive else " AND active=1"
        with self._connection() as conn:
            row = conn.execute(
                "SELECT group_id,user_id,nickname,card,qq_name,role "
                "FROM hive_monitor_members WHERE group_id=? AND user_id=?"
                + active_clause,
                (group, user),
            ).fetchone()
        return _row_to_member(row) if row is not None else None

    def member_active(self, group_id: int, user_id: int | str) -> bool:
        group = self._validated_group_id(group_id)
        user = _positive_decimal(user_id)
        if user is None:
            return False
        with self._connection() as conn:
            row = conn.execute(
                "SELECT active FROM hive_monitor_members WHERE group_id=? AND user_id=?",
                (group, user),
            ).fetchone()
        return bool(row is not None and int(row[0]) == 1)

    def member_episode_started_at(
        self,
        group_id: int,
        user_id: int | str,
    ) -> str | None:
        group = self._validated_group_id(group_id)
        user = _positive_decimal(user_id)
        if user is None:
            return None
        with self._connection() as conn:
            row = conn.execute(
                "SELECT episode_started_at FROM hive_monitor_members "
                "WHERE group_id=? AND user_id=?",
                (group, user),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def member_count(self, group_id: int) -> int:
        group = self._validated_group_id(group_id)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM hive_monitor_members WHERE group_id=? AND active=1",
                (group,),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    @staticmethod
    def _member_id_fingerprint(user_ids: Iterable[int | str]) -> str:
        normalized = {
            user_id
            for value in user_ids
            if (user_id := _positive_decimal(value)) is not None
        }
        material = "\n".join(sorted(normalized, key=int))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def observe_mass_difference_candidate(
        self,
        group_id: int,
        *,
        baseline_user_ids: Iterable[int | str],
        candidate_user_ids: Iterable[int | str],
        now: datetime | str | None = None,
    ) -> int:
        """Persist confirmation of one stable, complete large-change candidate."""

        group = self._validated_group_id(group_id)
        baseline = tuple(baseline_user_ids)
        candidate = tuple(candidate_user_ids)
        if not candidate:
            raise ValueError("mass difference candidate must not be empty")
        baseline_sha256 = self._member_id_fingerprint(baseline)
        candidate_sha256 = self._member_id_fingerprint(candidate)
        member_count = len(
            {
                user_id
                for value in candidate
                if (user_id := _positive_decimal(value)) is not None
            }
        )
        if member_count < 1:
            raise ValueError("mass difference candidate has no valid members")
        timestamp = _timestamp(now)

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT baseline_sha256,candidate_sha256,member_count,confirmations "
                "FROM hive_monitor_mass_candidates WHERE group_id=?",
                (group,),
            ).fetchone()
            same_candidate = bool(
                row is not None
                and str(row["baseline_sha256"]) == baseline_sha256
                and str(row["candidate_sha256"]) == candidate_sha256
                and int(row["member_count"]) == member_count
            )
            confirmations = min(
                3,
                (int(row["confirmations"]) + 1) if same_candidate else 1,
            )
            conn.execute(
                """
                INSERT INTO hive_monitor_mass_candidates(
                    group_id,baseline_sha256,candidate_sha256,member_count,
                    confirmations,updated_at
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(group_id) DO UPDATE SET
                    baseline_sha256=excluded.baseline_sha256,
                    candidate_sha256=excluded.candidate_sha256,
                    member_count=excluded.member_count,
                    confirmations=excluded.confirmations,
                    updated_at=excluded.updated_at
                """,
                (
                    group,
                    baseline_sha256,
                    candidate_sha256,
                    member_count,
                    confirmations,
                    timestamp,
                ),
            )
            conn.commit()
        return confirmations

    def clear_mass_difference_candidate(self, group_id: int) -> None:
        group = self._validated_group_id(group_id)
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM hive_monitor_mass_candidates WHERE group_id=?",
                (group,),
            )

    @staticmethod
    def _upsert_current(
        conn: sqlite3.Connection,
        group_id: int,
        members: Sequence[MemberSnapshot],
        timestamp: str,
    ) -> None:
        conn.executemany(
            """
            INSERT INTO hive_monitor_members(
                group_id,user_id,nickname,card,qq_name,role,active,
                missing_count,first_seen_at,episode_started_at,last_seen_at,left_at
            ) VALUES(?,?,?,?,?,?,1,0,?,?,?,NULL)
            ON CONFLICT(group_id,user_id) DO UPDATE SET
                nickname=excluded.nickname,
                card=excluded.card,
                qq_name=excluded.qq_name,
                role=excluded.role,
                episode_started_at=CASE
                    WHEN hive_monitor_members.active=0
                    THEN excluded.episode_started_at
                    ELSE hive_monitor_members.episode_started_at
                END,
                active=1,
                missing_count=0,
                last_seen_at=excluded.last_seen_at,
                left_at=NULL
            """,
            [
                (
                    group_id,
                    member.user_id,
                    member.nickname,
                    member.card,
                    member.qq_name,
                    member.role,
                    timestamp,
                    timestamp,
                    timestamp,
                )
                for member in members
            ],
        )

    @staticmethod
    def _touch_group_state(conn: sqlite3.Connection, group_id: int, timestamp: str) -> None:
        conn.execute(
            """
            INSERT INTO hive_monitor_group_state(
                group_id,initial_export_delivered_at,last_successful_sync_at,updated_at
            ) VALUES(?,NULL,?,?)
            ON CONFLICT(group_id) DO UPDATE SET
                last_successful_sync_at=excluded.last_successful_sync_at,
                updated_at=excluded.updated_at
            """,
            (group_id, timestamp, timestamp),
        )

    @staticmethod
    def _ensure_departure_row(
        conn: sqlite3.Connection,
        *,
        event_key: str,
        group_id: int,
        user_id: str,
        qq_name: str,
        sub_type: str,
        operator_id: str,
        event_time: int,
        source: str,
        timestamp: str,
    ) -> sqlite3.Row:
        conn.execute(
            """
            INSERT OR IGNORE INTO hive_monitor_departure_outbox(
                event_key,group_id,user_id,qq_name,sub_type,operator_id,event_time,source,
                status,attempt_count,last_error,lease_token,lease_until,
                created_at,updated_at,delivered_at
            ) VALUES(?,?,?,?,?,?,?,?,'pending',0,'','',NULL,?,?,NULL)
            """,
            (
                event_key,
                group_id,
                user_id,
                qq_name,
                sub_type,
                operator_id,
                event_time,
                source,
                timestamp,
                timestamp,
            ),
        )
        row = conn.execute(
            "SELECT * FROM hive_monitor_departure_outbox WHERE event_key=?",
            (event_key,),
        ).fetchone()
        if row is None:  # pragma: no cover - guarded by the transaction
            raise RuntimeError("failed to persist departure event")
        return row

    def replace_snapshot(
        self,
        group_id: int,
        members: Iterable[Mapping[str, object] | MemberSnapshot],
        *,
        now: datetime | str | None = None,
    ) -> SnapshotDelta:
        """Atomically replace the active snapshot; an empty result is rejected."""

        group = self._validated_group_id(group_id)
        normalized = normalize_members(members)
        if not normalized:
            raise ValueError("member snapshot must contain at least one valid member")
        timestamp = _timestamp(now)
        current_ids = {member.user_id for member in normalized}

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = {
                str(row["user_id"]): _row_to_member(row)
                for row in conn.execute(
                    "SELECT group_id,user_id,nickname,card,qq_name,role "
                    "FROM hive_monitor_members WHERE group_id=? AND active=1",
                    (group,),
                ).fetchall()
            }
            self._upsert_current(conn, group, normalized, timestamp)
            departed_ids = previous.keys() - current_ids
            if departed_ids:
                conn.executemany(
                    "UPDATE hive_monitor_members "
                    "SET active=0,left_at=?,missing_count=0 "
                    "WHERE group_id=? AND user_id=? AND active=1",
                    [
                        (timestamp, group, user_id)
                        for user_id in sorted(departed_ids, key=int)
                    ],
                )
            self._touch_group_state(conn, group, timestamp)
            conn.commit()

        joined = tuple(member for member in normalized if member.user_id not in previous)
        departed = tuple(previous[user_id] for user_id in previous.keys() - current_ids)
        return SnapshotDelta(joined=joined, departed=departed)

    def reconcile_snapshot(
        self,
        group_id: int,
        members: Iterable[Mapping[str, object] | MemberSnapshot],
        *,
        now: datetime | str | None = None,
        missing_threshold: int = 2,
        departure_sub_type: str | None = None,
        departure_event_time: int | None = None,
        departure_operator_id: int | str | None = None,
        departure_source: str = "OneBot",
    ) -> SnapshotDelta:
        """Observe a full list and declare departure only after repeated absence.

        When ``departure_sub_type`` is provided, every newly confirmed departure
        is also inserted into the outbox in this same transaction.
        """

        if int(missing_threshold) < 1:
            raise ValueError("missing_threshold must be at least 1")
        group = self._validated_group_id(group_id)
        normalized = normalize_members(members)
        if not normalized:
            raise ValueError("member snapshot must contain at least one valid member")
        timestamp = _timestamp(now)
        current_ids = {member.user_id for member in normalized}
        event_sub_type = (
            _clean_text(departure_sub_type) if departure_sub_type is not None else ""
        )
        event_source = _clean_text(departure_source) or "OneBot"
        event_operator_id = _positive_decimal(departure_operator_id) or ""
        if event_sub_type:
            if departure_event_time is not None:
                reconcile_event_time = int(departure_event_time)
            elif isinstance(now, datetime):
                reconcile_event_time = int(now.timestamp())
            elif now is not None:
                try:
                    reconcile_event_time = int(
                        datetime.fromisoformat(str(now).strip()).timestamp()
                    )
                except ValueError as exc:
                    raise ValueError("now must be an ISO datetime") from exc
            else:
                reconcile_event_time = int(datetime.now().timestamp())
        else:
            reconcile_event_time = 0

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous_rows = conn.execute(
                "SELECT group_id,user_id,nickname,card,qq_name,role,missing_count "
                "FROM hive_monitor_members WHERE group_id=? AND active=1",
                (group,),
            ).fetchall()
            previous = {str(row["user_id"]): row for row in previous_rows}
            self._upsert_current(conn, group, normalized, timestamp)

            departed: list[MemberSnapshot] = []
            for user_id, row in previous.items():
                if user_id in current_ids:
                    continue
                missing_count = int(row["missing_count"]) + 1
                if missing_count >= int(missing_threshold):
                    conn.execute(
                        "UPDATE hive_monitor_members SET active=0,missing_count=?,left_at=? "
                        "WHERE group_id=? AND user_id=? AND active=1",
                        (missing_count, timestamp, group, user_id),
                    )
                    member = _row_to_member(row)
                    departed.append(member)
                    if event_sub_type:
                        key = departure_event_key(
                            group,
                            member.user_id,
                            event_sub_type,
                            reconcile_event_time,
                            source=event_source,
                        )
                        self._ensure_departure_row(
                            conn,
                            event_key=key,
                            group_id=group,
                            user_id=member.user_id,
                            qq_name=member.qq_name,
                            sub_type=event_sub_type,
                            operator_id=event_operator_id,
                            event_time=reconcile_event_time,
                            source=event_source,
                            timestamp=timestamp,
                        )
                else:
                    conn.execute(
                        "UPDATE hive_monitor_members SET missing_count=? "
                        "WHERE group_id=? AND user_id=? AND active=1",
                        (missing_count, group, user_id),
                    )
            self._touch_group_state(conn, group, timestamp)
            conn.commit()

        joined = tuple(member for member in normalized if member.user_id not in previous)
        return SnapshotDelta(joined=joined, departed=tuple(departed))

    def reconcile_snapshot_with_departures(
        self,
        group_id: int,
        members: Iterable[Mapping[str, object] | MemberSnapshot],
        *,
        now: datetime | str | None = None,
        missing_threshold: int = 2,
        event_time: int | None = None,
        sub_type: str = "reconcile",
        operator_id: int | str | None = None,
        source: str = "OneBot V11 member reconciliation",
    ) -> SnapshotDelta:
        """Reconcile members and atomically enqueue every confirmed departure."""

        event_sub_type = _clean_text(sub_type)
        if not event_sub_type:
            raise ValueError("sub_type is required")
        return self.reconcile_snapshot(
            group_id,
            members,
            now=now,
            missing_threshold=missing_threshold,
            departure_sub_type=event_sub_type,
            departure_event_time=event_time,
            departure_operator_id=operator_id,
            departure_source=source,
        )

    reconcile_snapshot_and_enqueue_departures = reconcile_snapshot_with_departures

    def upsert_member(
        self,
        group_id: int,
        member: Mapping[str, object] | MemberSnapshot,
        *,
        now: datetime | str | None = None,
    ) -> MemberSnapshot:
        group = self._validated_group_id(group_id)
        normalized = normalize_members([member])
        if not normalized:
            raise ValueError("member row is invalid")
        timestamp = _timestamp(now)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._upsert_current(conn, group, normalized, timestamp)
            conn.commit()
        return normalized[0]

    def mark_member_left(
        self,
        group_id: int,
        user_id: int | str,
        *,
        now: datetime | str | None = None,
    ) -> MemberSnapshot | None:
        group = self._validated_group_id(group_id)
        user = _positive_decimal(user_id)
        if user is None:
            return None
        timestamp = _timestamp(now)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT group_id,user_id,nickname,card,qq_name,role "
                "FROM hive_monitor_members WHERE group_id=? AND user_id=?",
                (group, user),
            ).fetchone()
            conn.execute(
                "UPDATE hive_monitor_members SET active=0,left_at=?,missing_count=0 "
                "WHERE group_id=? AND user_id=?",
                (timestamp, group, user),
            )
            conn.commit()
        return _row_to_member(row) if row is not None else None

    def mark_left_and_ensure_departure(
        self,
        *,
        event_key: str,
        group_id: int,
        user_id: int | str,
        qq_name: str,
        sub_type: str,
        event_time: int,
        operator_id: int | str | None = None,
        source: str = "OneBot",
        now: datetime | str | None = None,
        expected_episode_started_at: str | None = None,
    ) -> DepartureEvent | None:
        """Atomically close one membership episode and persist its outbox row.

        Replaying the same event, or a delayed event for an already closed
        membership episode, never marks a rejoined member inactive.  When an
        expected watermark is supplied, a newer member observation aborts both
        mutations and returns ``None``.
        """

        key = str(event_key).strip()
        if not key:
            raise ValueError("event_key is required")
        group = self._validated_group_id(group_id)
        user = _positive_decimal(user_id)
        if user is None:
            raise ValueError("user_id must be positive")
        timestamp = _timestamp(now)
        event_sub_type = _clean_text(sub_type) or "unknown"
        event_operator_id = _positive_decimal(operator_id) or ""
        event_source = _clean_text(source) or "OneBot"

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM hive_monitor_departure_outbox WHERE event_key=?",
                (key,),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return _row_to_departure(existing)

            member_row = conn.execute(
                "SELECT group_id,user_id,nickname,card,qq_name,role,active,"
                "episode_started_at "
                "FROM hive_monitor_members WHERE group_id=? AND user_id=?",
                (group, user),
            ).fetchone()
            if (
                expected_episode_started_at is not None
                and (
                    member_row is None
                    or str(member_row["episode_started_at"])
                    != str(expected_episode_started_at)
                )
            ):
                conn.commit()
                return None

            if member_row is not None and int(member_row["active"]) == 0:
                current_episode = conn.execute(
                    "SELECT * FROM hive_monitor_departure_outbox "
                    "WHERE group_id=? AND user_id=? "
                    "ORDER BY event_time DESC,created_at DESC,event_key DESC LIMIT 1",
                    (group, user),
                ).fetchone()
                if current_episode is not None:
                    conn.commit()
                    return _row_to_departure(current_episode)

            name = _clean_text(qq_name)
            if not name and member_row is not None:
                name = str(member_row["qq_name"])
            row = self._ensure_departure_row(
                conn,
                event_key=key,
                group_id=group,
                user_id=user,
                qq_name=name or user,
                sub_type=event_sub_type,
                operator_id=event_operator_id,
                event_time=int(event_time),
                source=event_source,
                timestamp=timestamp,
            )
            conn.execute(
                "UPDATE hive_monitor_members "
                "SET active=0,left_at=?,missing_count=0 "
                "WHERE group_id=? AND user_id=? AND active=1",
                (timestamp, group, user),
            )
            conn.commit()
        return _row_to_departure(row)

    mark_member_left_and_ensure_departure = mark_left_and_ensure_departure

    def initial_export_delivered(
        self,
        group_id: int,
        report_group_id: int | None = None,
    ) -> bool:
        group = self._validated_group_id(group_id)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT initial_export_delivered_at,initial_export_report_group_id "
                "FROM hive_monitor_group_state WHERE group_id=?",
                (group,),
            ).fetchone()
        if row is None or not row[0]:
            return False
        if report_group_id is None:
            return True
        return row[1] is not None and int(row[1]) == self._validated_group_id(
            report_group_id
        )

    def mark_initial_export_delivered(
        self,
        group_id: int,
        report_group_id: int | None = None,
        file_name: str = "",
        sha256: str = "",
        *,
        now: datetime | str | None = None,
    ) -> None:
        group = self._validated_group_id(group_id)
        report_group = (
            None
            if report_group_id is None
            else self._validated_group_id(report_group_id)
        )
        timestamp = _timestamp(now)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO hive_monitor_group_state(
                    group_id,initial_export_delivered_at,
                    initial_export_report_group_id,initial_export_file_name,
                    initial_export_sha256,last_successful_sync_at,updated_at
                ) VALUES(?,?,?,?,?,NULL,?)
                ON CONFLICT(group_id) DO UPDATE SET
                    initial_export_delivered_at=excluded.initial_export_delivered_at,
                    initial_export_report_group_id=excluded.initial_export_report_group_id,
                    initial_export_file_name=excluded.initial_export_file_name,
                    initial_export_sha256=excluded.initial_export_sha256,
                    updated_at=excluded.updated_at
                """,
                (
                    group,
                    timestamp,
                    report_group,
                    _clean_text(file_name),
                    _clean_text(sha256),
                    timestamp,
                ),
            )
            conn.commit()

    # Compatibility alias for service code that expresses this as a setter.
    set_initial_export_delivered = mark_initial_export_delivered

    def ensure_departure_event(
        self,
        *,
        event_key: str,
        group_id: int,
        user_id: int | str,
        qq_name: str,
        sub_type: str,
        operator_id: int | str | None = None,
        event_time: int,
        source: str = "OneBot",
        now: datetime | str | None = None,
    ) -> DepartureEvent:
        key = str(event_key).strip()
        if not key:
            raise ValueError("event_key is required")
        group = self._validated_group_id(group_id)
        user = _positive_decimal(user_id)
        if user is None:
            raise ValueError("user_id must be positive")
        timestamp = _timestamp(now)
        name = _clean_text(qq_name) or user
        event_sub_type = _clean_text(sub_type) or "unknown"
        event_operator_id = _positive_decimal(operator_id) or ""
        event_source = _clean_text(source) or "OneBot"
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._ensure_departure_row(
                conn,
                event_key=key,
                group_id=group,
                user_id=user,
                qq_name=name,
                sub_type=event_sub_type,
                operator_id=event_operator_id,
                event_time=int(event_time),
                source=event_source,
                timestamp=timestamp,
            )
            conn.commit()
        return _row_to_departure(row)

    def get_departure_event(self, event_key: str) -> DepartureEvent | None:
        key = str(event_key).strip()
        if not key:
            return None
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM hive_monitor_departure_outbox WHERE event_key=?",
                (key,),
            ).fetchone()
        return _row_to_departure(row) if row is not None else None

    def latest_departure_for_member(
        self,
        group_id: int,
        user_id: int | str,
    ) -> DepartureEvent | None:
        group = self._validated_group_id(group_id)
        user = _positive_decimal(user_id)
        if user is None:
            return None
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM hive_monitor_departure_outbox "
                "WHERE group_id=? AND user_id=? "
                "ORDER BY event_time DESC,created_at DESC,event_key DESC LIMIT 1",
                (group, user),
            ).fetchone()
        return _row_to_departure(row) if row is not None else None

    def departure_delivered(self, event_key: str) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT status FROM hive_monitor_departure_outbox WHERE event_key=?",
                (str(event_key).strip(),),
            ).fetchone()
        return bool(row is not None and row[0] == "delivered")

    def list_pending_departures(self, *, group_id: int | None = None) -> list[DepartureEvent]:
        params: tuple[object, ...]
        if group_id is None:
            where = "status='pending'"
            params = ()
        else:
            where = "status='pending' AND group_id=?"
            params = (self._validated_group_id(group_id),)
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM hive_monitor_departure_outbox WHERE " + where
                + " ORDER BY created_at,event_key",
                params,
            ).fetchall()
        return [_row_to_departure(row) for row in rows]

    def claim_pending_departures(
        self,
        group_id: int,
        lease_token: str,
        now: datetime | str | None = None,
        lease_seconds: int = 60,
        limit: int = 20,
    ) -> list[DepartureEvent]:
        """Atomically lease retryable events to one worker.

        Rows with an unexpired lease are skipped.  An expired lease can be
        reclaimed, so a worker crash cannot strand a pending notification.
        """

        group = self._validated_group_id(group_id)
        token = str(lease_token).strip()
        seconds = int(lease_seconds)
        row_limit = min(int(limit), 1000)
        if not token:
            raise ValueError("lease_token is required")
        if seconds < 1:
            raise ValueError("lease_seconds must be at least 1")
        if row_limit < 1:
            raise ValueError("limit must be at least 1")
        if isinstance(now, datetime):
            current = now
        elif now is None:
            current = datetime.now()
        else:
            try:
                current = datetime.fromisoformat(str(now).strip())
            except ValueError as exc:
                raise ValueError("now must be an ISO datetime") from exc
        now_text = _timestamp(current)
        lease_until = _timestamp(current + timedelta(seconds=seconds))

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            candidate_rows = conn.execute(
                "SELECT event_key FROM hive_monitor_departure_outbox "
                "WHERE group_id=? AND status='pending' "
                "AND (lease_token='' OR lease_until IS NULL OR lease_until<=?) "
                "ORDER BY created_at,event_key LIMIT ?",
                (group, now_text, row_limit),
            ).fetchall()
            keys = [str(row["event_key"]) for row in candidate_rows]
            if not keys:
                conn.commit()
                return []

            placeholders = ",".join("?" for _ in keys)
            conn.execute(
                "UPDATE hive_monitor_departure_outbox "
                "SET lease_token=?,lease_until=?,updated_at=? "
                f"WHERE event_key IN ({placeholders}) AND status='pending' "
                "AND (lease_token='' OR lease_until IS NULL OR lease_until<=?)",
                (token, lease_until, now_text, *keys, now_text),
            )
            claimed_rows = conn.execute(
                "SELECT * FROM hive_monitor_departure_outbox "
                f"WHERE event_key IN ({placeholders}) AND status='pending' "
                "AND lease_token=? ORDER BY created_at,event_key",
                (*keys, token),
            ).fetchall()
            conn.commit()
        return [_row_to_departure(row) for row in claimed_rows]

    def mark_departure_delivered(
        self,
        event_key: str,
        lease_token: str | None = None,
        *,
        now: datetime | str | None = None,
    ) -> bool:
        timestamp = _timestamp(now)
        key = str(event_key).strip()
        token = None if lease_token is None else str(lease_token).strip()
        if lease_token is not None and not token:
            return False
        # Legacy callers may settle only an unclaimed row.  Once a worker has
        # leased an event, omitting the token must not bypass lease ownership.
        lease_clause = " AND lease_token=''" if token is None else " AND lease_token=?"
        params: tuple[object, ...] = (
            (timestamp, timestamp, key)
            if token is None
            else (timestamp, timestamp, key, token)
        )
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE hive_monitor_departure_outbox "
                "SET status='delivered',attempt_count=attempt_count+1,last_error='',"
                "lease_token='',lease_until=NULL,delivered_at=?,updated_at=? "
                "WHERE event_key=? AND status='pending'" + lease_clause,
                params,
            )
        return cursor.rowcount == 1

    def mark_departure_failed(
        self,
        event_key: str,
        error: object,
        lease_token: str | None = None,
        *,
        now: datetime | str | None = None,
    ) -> bool:
        timestamp = _timestamp(now)
        detail = str(error).strip()[:1000]
        key = str(event_key).strip()
        token = None if lease_token is None else str(lease_token).strip()
        if lease_token is not None and not token:
            return False
        lease_clause = " AND lease_token=''" if token is None else " AND lease_token=?"
        params: tuple[object, ...] = (
            (detail, timestamp, key)
            if token is None
            else (detail, timestamp, key, token)
        )
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE hive_monitor_departure_outbox "
                "SET attempt_count=attempt_count+1,last_error=?,"
                "lease_token='',lease_until=NULL,updated_at=? "
                "WHERE event_key=? AND status='pending'" + lease_clause,
                params,
            )
        return cursor.rowcount == 1

    def release_departure_claim(
        self,
        event_key: str,
        lease_token: str,
        *,
        now: datetime | str | None = None,
    ) -> bool:
        key = str(event_key).strip()
        token = str(lease_token).strip()
        if not key or not token:
            return False
        timestamp = _timestamp(now)
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE hive_monitor_departure_outbox "
                "SET lease_token='',lease_until=NULL,updated_at=? "
                "WHERE event_key=? AND status='pending' AND lease_token=?",
                (timestamp, key, token),
            )
        return cursor.rowcount == 1

    release_claim = release_departure_claim
