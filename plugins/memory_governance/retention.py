"""Retain governance audit metadata while expiring plaintext operation previews."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3


def prune_previews(path: Path, *, now: datetime, retention_days: int = 7) -> int:
    if now.tzinfo is None or type(retention_days) is not int or retention_days < 1:
        raise ValueError('retention requires an aware time and positive days')
    if not Path(path).is_file():
        return 0
    cutoff=(now.astimezone(timezone.utc)-timedelta(days=retention_days)).isoformat()
    with closing(sqlite3.connect(path)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_pending_operations'").fetchone():
            return 0
        connection.execute('PRAGMA secure_delete=ON')
        cursor=connection.execute(
            "UPDATE memory_pending_operations SET payload_json='{}',preview_text='' "
            "WHERE julianday(COALESCE(consumed_at,expires_at))<julianday(?) "
            "AND (payload_json<>'{}' OR preview_text<>'')", (cutoff,),
        )
        connection.commit()
        # PASSIVE never waits for live readers; later cycles reclaim WAL pages.
        connection.execute('PRAGMA wal_checkpoint(PASSIVE)')
        return int(cursor.rowcount)


def clear_delivery_plans(connection: sqlite3.Connection, user_id: str) -> None:
    """Called within the same clear transaction; preserve opaque dedupe tombstones."""
    exists=connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_delivery_parts'").fetchone()
    if exists:
        connection.execute(
            "UPDATE chat_delivery_parts SET reply_text='',receipt='',error='',status='cancelled',user_id='' "
            "WHERE kind='private' AND user_id=?", (user_id,),
        )


def invalidate_fact_progress(connection: sqlite3.Connection, user_id: str, through: int, now: str) -> None:
    connection.execute(
        "INSERT INTO private_fact_progress(user_id,through_message_id,version,updated_at) VALUES(?,?,1,?) "
        "ON CONFLICT(user_id) DO UPDATE SET through_message_id=excluded.through_message_id,"
        "version=private_fact_progress.version+1,updated_at=excluded.updated_at", (user_id,through,now),
    )
