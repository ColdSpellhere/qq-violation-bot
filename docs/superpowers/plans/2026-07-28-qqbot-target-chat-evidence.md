# QQ Bot Target Chat and Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Archive only the configured target group, persist every image referenced while recording a violation, bind those images to the confirmed record, and return each record with all of its evidence without changing existing query selection or business calculations.

**Architecture:** A passive target-only archive writes message metadata to its own SQLite database. A separate evidence store owns content-addressed image files and external `violation_id` mappings; the business transaction commits first, then a durable binding queue completes the sidecar association. Query functions keep their SQL and produce a structured display envelope consumed by the matcher.

**Tech Stack:** Python 3.10, NoneBot2 2.5, OneBot v11, SQLite, httpx, `unittest`

---

## Execution Boundary

Run only after the public repository baseline plan passes. Do not edit
`ai_router.py`, `schemas.py`, `validators.py`, member resolution, state
calculation, query predicates, query ordering, or automatic deduction logic.
Tests use temporary databases and files; no test writes to production data.

## File Map

- Create: `plugins/chat_archive/__init__.py` - plugin entrypoint.
- Create: `plugins/chat_archive/db.py` - target-only archive schema and inserts.
- Create: `plugins/chat_archive/matcher.py` - passive exact-group handler.
- Create: `plugins/violation_record/evidence_store.py` - evidence metadata, files, binding queue, cleanup.
- Create: `plugins/violation_record/evidence_capture.py` - referenced-message extraction and bounded image download.
- Create: `plugins/violation_record/reply_models.py` - structured record messages.
- Modify: `bot.py` - load archive before the business matcher.
- Modify: `plugins/violation_record/config.py` - evidence, archive, and mute settings.
- Modify: `plugins/violation_record/matcher.py` - capture evidence and send mixed messages.
- Modify: `plugins/violation_record/service.py` - carry batch IDs, expose inserted record ID, structure query output.
- Modify: `plugins/violation_record/moderation.py` - fail closed when mute is disabled.
- Modify: `plugins/violation_record/scheduler.py` - retry bindings and clean transient evidence.
- Modify: `.env.example`, `README.md` - document new settings and behavior.
- Create/modify: `tests/test_query_contract.py`, `tests/test_chat_archive.py`, `tests/test_evidence_store.py`, `tests/test_evidence_capture.py`, `tests/test_evidence_service.py`, `tests/test_reply_delivery.py`, `tests/test_mute_switch.py`.

### Task 1: Lock Existing Query Semantics Before Refactoring

**Files:**
- Create: `tests/test_query_contract.py`

- [ ] **Step 1: Add a temporary business-database fixture and golden assertions**

Create `tests/test_query_contract.py`. The fixture must patch only
`plugins.violation_record.db.CONFIG` with `dataclasses.replace`, call `init_db()`
against a temporary path, and insert one operator, one member, one state row, and
two violation rows with distinct times. Use this test body:

```python
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from plugins.violation_record import db, service
from plugins.violation_record.config import CONFIG


class QueryContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        database_path = Path(self.temp.name) / "business.db"
        self.config_patch = patch.object(
            db,
            "CONFIG",
            replace(
                CONFIG,
                database_path=database_path,
                database_url=f"sqlite:///{database_path}",
            ),
        )
        self.config_patch.start()
        db.init_db()
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO admins(qq_number,nickname,aliases,is_active,created_at,updated_at) VALUES('90001','记录员','[]',1,?,?)",
                (db.now_str(), db.now_str()),
            )
            conn.execute(
                "INSERT INTO members(qq_number,qq_nickname,aliases,created_at,updated_at) VALUES('123456','小明','[]',?,?)",
                (db.now_str(), db.now_str()),
            )
            member_id = conn.execute(
                "SELECT id FROM members WHERE qq_number='123456'"
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO member_group_states(member_id,group_area,status,locked,total_count,deduct_count,current_count_cache,created_at,updated_at) VALUES(?, '蜂巢', '正常', 0, 0, 0, 0, ?, ?)",
                (member_id, db.now_str(), db.now_str()),
            )
            for when, judgement, action in (
                ("2026-07-02 10:00:00", "刷屏", "禁言10分钟"),
                ("2026-07-01 09:00:00", "引战", "警告"),
            ):
                conn.execute(
                    "INSERT INTO violation_records(member_id,group_area,violation_time,judgement,action,remark,is_countable,count_delta,is_test,created_at,updated_at) VALUES(?, '蜂巢', ?, ?, ?, '无', 1, 1, 0, ?, ?)",
                    (member_id, when, judgement, action, db.now_str(), db.now_str()),
                )

    def tearDown(self) -> None:
        self.config_patch.stop()
        self.temp.cleanup()

    def test_member_query_text_and_order_are_locked(self) -> None:
        intent = {
            "group_area": "蜂巢",
            "target": {"qq_number": "123456", "qq_nickname": None},
            "query": {"recent_days": 14},
        }
        result = service.query_member(intent, "90001", "记录员", False, "m1")
        self.assertEqual(
            "小明（123456）\n\n当前次数：2\n状态：正常\n\n具体记录：\n\n"
            "1. 2026-07-02 10:00，刷屏，禁言10分钟\n"
            "2. 2026-07-01 09:00，引战，警告",
            result,
        )

    def test_area_query_text_and_order_are_locked(self) -> None:
        intent = {"group_area": "蜂巢", "query": {"time_range": "all", "limit": 20}, "_raw": "蜂巢违规记录"}
        result = service.query_area_records(intent, "90001", "记录员", "m2")
        self.assertEqual(
            "蜂巢全部违规记录\n\n记录数：2\n\n具体记录：\n\n"
            "1. 小明（123456） 2026-07-02 10:00，刷屏，禁言10分钟\n"
            "2. 小明（123456） 2026-07-01 09:00，引战，警告",
            result,
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the golden tests against untouched query code**

```bash
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest tests.test_query_contract -v
```

Expected: `Ran 2 tests` and `OK`. If the literal expected text differs, compare it
to the current production function and correct only the fixture expectation; do
not alter production query code.

- [ ] **Step 3: Commit the golden contract**

```bash
git add tests/test_query_contract.py
git commit -m "test: lock existing query behavior"
```

### Task 2: Add the Target-Only Chat Archive

**Files:**
- Create: `plugins/chat_archive/__init__.py`
- Create: `plugins/chat_archive/db.py`
- Create: `plugins/chat_archive/matcher.py`
- Modify: `bot.py`
- Modify: `plugins/violation_record/config.py`
- Test: `tests/test_chat_archive.py`

- [ ] **Step 1: Write failing archive tests**

Create `tests/test_chat_archive.py` with tests that call `archive_payload()` twice
with the same target `message_id` and once with a different group. Assert one row
exists and that the non-target call returns `False`:

```python
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from plugins.chat_archive.db import archive_payload


class ChatArchiveTests(unittest.TestCase):
    def test_only_target_group_is_archived_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat.db"
            payload = {
                "message_id": "101",
                "group_id": 123456789,
                "event_time": 1785168000,
                "user_id": "456789",
                "sender": {"card": "记录员"},
                "segments": [{"type": "text", "data": {"text": "证据"}}],
                "plaintext": "证据",
                "reply_message_id": "99",
            }
            self.assertTrue(archive_payload(path, 123456789, payload))
            self.assertTrue(archive_payload(path, 123456789, payload))
            outside = dict(payload, message_id="102", group_id=987654321)
            self.assertFalse(archive_payload(path, 123456789, outside))
            with sqlite3.connect(path) as conn:
                row = conn.execute(
                    "SELECT group_id,user_id,message_json,reply_message_id FROM chat_messages"
                ).fetchone()
                count = conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
            self.assertEqual(1, count)
            self.assertEqual((123456789, "456789"), row[:2])
            self.assertIn('"type": "text"', row[2])
            self.assertEqual("99", row[3])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the archive test and verify it fails**

```bash
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest tests.test_chat_archive -v
```

Expected: import failure because `plugins.chat_archive` does not exist.

- [ ] **Step 3: Add the archive path to application config**

Add to `AppConfig`:

```python
    chat_archive_path: Path = DATA_DIR / "chat_archive.db"
```

- [ ] **Step 4: Implement the archive database**

Create `plugins/chat_archive/db.py`:

```python
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    message_id TEXT PRIMARY KEY,
    group_id INTEGER NOT NULL,
    event_time INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    sender_json TEXT NOT NULL,
    message_json TEXT NOT NULL,
    plaintext TEXT NOT NULL,
    reply_message_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_time
ON chat_messages(event_time, message_id);
"""


def archive_payload(path: Path, target_group_id: int, payload: dict[str, Any]) -> bool:
    if int(payload["group_id"]) != target_group_id:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            """
            INSERT INTO chat_messages(
                message_id,group_id,event_time,user_id,sender_json,message_json,
                plaintext,reply_message_id,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(message_id) DO NOTHING
            """,
            (
                str(payload["message_id"]),
                target_group_id,
                int(payload["event_time"]),
                str(payload["user_id"]),
                json.dumps(payload["sender"], ensure_ascii=False, default=str),
                json.dumps(payload["segments"], ensure_ascii=False, default=str),
                str(payload.get("plaintext") or ""),
                str(payload.get("reply_message_id") or "") or None,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
    path.chmod(0o600)
    return True
```

- [ ] **Step 5: Implement the passive matcher**

Create `plugins/chat_archive/matcher.py`:

```python
from __future__ import annotations

from typing import Any

from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import Event, GroupMessageEvent
from nonebot.rule import Rule

from plugins.violation_record.config import CONFIG
from .db import archive_payload


def _reply_id(event: GroupMessageEvent) -> str | None:
    for segment in event.message:
        if segment.type == "reply":
            value = segment.data.get("id") or segment.data.get("message_id")
            return str(value) if value is not None else None
    return None


def _sender_dict(event: GroupMessageEvent) -> dict[str, Any]:
    sender = event.sender
    if hasattr(sender, "model_dump"):
        return sender.model_dump()
    if hasattr(sender, "dict"):
        return sender.dict()
    return {"nickname": getattr(sender, "nickname", None), "card": getattr(sender, "card", None)}


def _target_group(event: Event) -> bool:
    return isinstance(event, GroupMessageEvent) and int(event.group_id) == CONFIG.target_group_id


archive_matcher = on_message(rule=Rule(_target_group), priority=1, block=False)


@archive_matcher.handle()
async def archive_target_message(event: GroupMessageEvent) -> None:
    try:
        archive_payload(
            CONFIG.chat_archive_path,
            CONFIG.target_group_id,
            {
                "message_id": str(event.message_id),
                "group_id": int(event.group_id),
                "event_time": int(event.time),
                "user_id": str(event.user_id),
                "sender": _sender_dict(event),
                "segments": [
                    {"type": segment.type, "data": dict(segment.data)}
                    for segment in event.message
                ],
                "plaintext": event.get_plaintext(),
                "reply_message_id": _reply_id(event),
            },
        )
    except Exception as exc:
        logger.warning(
            f"目标群归档失败 stage=archive message_id={event.message_id} error={type(exc).__name__}"
        )
```

Create `plugins/chat_archive/__init__.py`:

```python
from . import matcher as matcher
```

Load it before the business plugin in `bot.py`:

```python
nonebot.load_plugin("plugins.chat_archive")
nonebot.load_plugin("plugins.violation_record")
```

- [ ] **Step 6: Run archive and query contract tests**

```bash
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest tests.test_chat_archive tests.test_query_contract -v
```

Expected: all four tests pass.

- [ ] **Step 7: Commit the archive module**

```bash
git add bot.py plugins/chat_archive plugins/violation_record/config.py tests/test_chat_archive.py
git diff --cached --check
git commit -m "feat: archive target group messages"
```

### Task 3: Implement the Evidence Store and Safe Downloader

**Files:**
- Create: `plugins/violation_record/evidence_store.py`
- Create: `plugins/violation_record/evidence_capture.py`
- Modify: `plugins/violation_record/config.py`
- Test: `tests/test_evidence_store.py`
- Test: `tests/test_evidence_capture.py`

- [ ] **Step 1: Write failing storage lifecycle tests**

Create `tests/test_evidence_store.py` covering these exact assertions:

```python
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from plugins.violation_record.evidence_store import EvidenceStore


JPEG = b"\xff\xd8\xff\xe0" + (b"x" * 32) + b"\xff\xd9"
JPEG_2 = b"\xff\xd8\xff\xe1" + (b"y" * 32) + b"\xff\xd9"


class EvidenceStoreTests(unittest.TestCase):
    def test_one_record_binds_multiple_deduplicated_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            store = EvidenceStore(Path(directory) / "evidence.db", root)
            batch = store.create_batch(123456789, "90001", "cmd-1")
            first = store.add_bytes(batch, JPEG, "image/jpeg", 123456789, "src-1", 1)
            second = store.add_bytes(batch, JPEG, "image/jpeg", 123456789, "src-1", 2)
            store.add_bytes(batch, JPEG_2, "image/jpeg", 123456789, "src-1", 3)
            self.assertEqual(first, second)
            store.bind_batch(batch, 42, "123456")
            paths = store.paths_for_violations([42])
            self.assertEqual(2, len(paths[42]))
            self.assertTrue(all(path.is_file() for path in paths[42]))

    def test_old_record_without_mapping_returns_empty_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = EvidenceStore(Path(directory) / "evidence.db", Path(directory) / "evidence")
            self.assertEqual({7: ()}, store.paths_for_violations([7]))

    def test_binding_queue_retries_and_bound_file_survives_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            store = EvidenceStore(Path(directory) / "evidence.db", root)
            batch = store.create_batch(123456789, "90001", "cmd-2")
            store.add_bytes(batch, JPEG, "image/jpeg", 123456789, "src-2", 1)
            queue_path = store.queue_binding(batch, 84, "654321")
            self.assertTrue(queue_path.is_file())
            self.assertEqual(1, store.retry_binding_queue())
            bound_path = store.paths_for_violations([84])[84][0]
            store.cleanup_transient()
            self.assertTrue(bound_path.is_file())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Write failing download validation tests**

Create `tests/test_evidence_capture.py`:

```python
from __future__ import annotations

import unittest

import httpx

from plugins.violation_record.evidence_capture import download_image


JPEG = b"\xff\xd8\xff\xe0" + (b"x" * 32) + b"\xff\xd9"
PUBLIC_RESOLVER = lambda host: ["93.184.216.34"]


class EvidenceCaptureTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, content: bytes, mime_type: str) -> httpx.AsyncClient:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": mime_type},
                content=content,
            )
        )
        return httpx.AsyncClient(transport=transport)

    async def test_valid_jpeg_is_returned(self) -> None:
        async with self._client(JPEG, "image/jpeg") as client:
            downloaded = await download_image(
                "https://multimedia.nt.qq.com.cn/evidence.jpg",
                client=client,
                resolver=PUBLIC_RESOLVER,
                max_bytes=1024,
            )
        self.assertEqual("image/jpeg", downloaded.mime_type)
        self.assertEqual(JPEG, downloaded.content)

    async def test_private_destination_is_rejected(self) -> None:
        async with self._client(JPEG, "image/jpeg") as client:
            with self.assertRaisesRegex(ValueError, "non-public"):
                await download_image(
                    "https://example.invalid/evidence.jpg",
                    client=client,
                    resolver=lambda host: ["127.0.0.1"],
                    max_bytes=1024,
                )

    async def test_non_image_body_is_rejected(self) -> None:
        async with self._client(b"not-an-image", "text/plain") as client:
            with self.assertRaisesRegex(ValueError, "supported image"):
                await download_image(
                    "https://multimedia.nt.qq.com.cn/evidence.txt",
                    client=client,
                    resolver=PUBLIC_RESOLVER,
                    max_bytes=1024,
                )

    async def test_oversized_image_is_rejected(self) -> None:
        async with self._client(JPEG * 100, "image/jpeg") as client:
            with self.assertRaisesRegex(ValueError, "size limit"):
                await download_image(
                    "https://multimedia.nt.qq.com.cn/large.jpg",
                    client=client,
                    resolver=PUBLIC_RESOLVER,
                    max_bytes=32,
                )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run both files and verify imports fail**

```bash
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest tests.test_evidence_store tests.test_evidence_capture -v
```

Expected: import failures for the two new modules.

- [ ] **Step 4: Add evidence configuration**

Add these helpers and fields to `config.py`:

```python
def _bool_env(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


    evidence_database_path: Path = DATA_DIR / "evidence.db"
    evidence_root: Path = BASE_DIR / "evidence"
    evidence_required: bool = _bool_env("EVIDENCE_REQUIRED", False)
    evidence_max_bytes: int = _int_env("EVIDENCE_MAX_BYTES", 20 * 1024 * 1024)
```

The indented lines belong inside `AppConfig`.

- [ ] **Step 5: Implement `EvidenceStore` with the approved schema**

Create `evidence_store.py` with this complete implementation:

```python
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4


MIME_SUFFIX = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    relative_path TEXT NOT NULL UNIQUE,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    source_group_id INTEGER NOT NULL,
    source_message_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'stored'
);
CREATE TABLE IF NOT EXISTS evidence_batches (
    id TEXT PRIMARY KEY,
    group_id INTEGER NOT NULL,
    operator_qq TEXT NOT NULL,
    command_message_id TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    violation_id INTEGER,
    target_qq TEXT
);
CREATE TABLE IF NOT EXISTS evidence_batch_items (
    batch_id TEXT NOT NULL,
    evidence_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY(batch_id, ordinal),
    FOREIGN KEY(batch_id) REFERENCES evidence_batches(id),
    FOREIGN KEY(evidence_id) REFERENCES evidence_files(id)
);
CREATE TABLE IF NOT EXISTS violation_evidence (
    violation_id INTEGER NOT NULL,
    target_qq TEXT NOT NULL,
    evidence_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    bound_at TEXT NOT NULL,
    PRIMARY KEY(violation_id, evidence_id),
    FOREIGN KEY(evidence_id) REFERENCES evidence_files(id)
);
CREATE INDEX IF NOT EXISTS idx_violation_evidence_record
ON violation_evidence(violation_id, ordinal);
"""


def _now(value: datetime | None = None) -> str:
    return (value or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def write_binding_queue(root: Path, batch_id: str, violation_id: int, target_qq: str) -> Path:
    queue = Path(root) / "bind-queue"
    queue.mkdir(parents=True, exist_ok=True)
    queue.chmod(0o700)
    destination = queue / f"{batch_id}.json"
    temporary = queue / f"{batch_id}.{os.getpid()}.part"
    temporary.write_text(
        json.dumps({"batch_id": batch_id, "violation_id": violation_id, "target_qq": target_qq}),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(destination)
    return destination


class EvidenceStore:
    def __init__(self, database_path: Path, root: Path):
        self.database_path = Path(database_path)
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.database_path.exists():
            descriptor = os.open(
                self.database_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            os.close(descriptor)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
        self.database_path.chmod(0o600)

    def create_batch(self, group_id: int, operator_qq: str, command_message_id: str) -> str:
        batch_id = uuid4().hex
        created = datetime.now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO evidence_batches(id,group_id,operator_qq,command_message_id,state,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                (
                    batch_id,
                    group_id,
                    operator_qq,
                    command_message_id,
                    "staging",
                    _now(created),
                    _now(created + timedelta(minutes=3)),
                ),
            )
        return batch_id

    def add_bytes(
        self,
        batch_id: str,
        content: bytes,
        mime_type: str,
        source_group_id: int,
        source_message_id: str,
        ordinal: int,
    ) -> int:
        suffix = MIME_SUFFIX[mime_type]
        digest = hashlib.sha256(content).hexdigest()
        relative = Path("images") / digest[:2] / f"{digest}{suffix}"
        absolute = self.root / relative
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.parent.chmod(0o700)
        if not absolute.exists():
            temporary = absolute.with_name(f"{absolute.name}.{os.getpid()}.part")
            temporary.write_bytes(content)
            temporary.chmod(0o600)
            temporary.replace(absolute)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO evidence_files(sha256,relative_path,mime_type,size_bytes,source_group_id,source_message_id,created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(sha256) DO NOTHING",
                (digest, str(relative), mime_type, len(content), source_group_id, source_message_id, _now()),
            )
            evidence_id = int(
                conn.execute("SELECT id FROM evidence_files WHERE sha256=?", (digest,)).fetchone()["id"]
            )
            conn.execute(
                "INSERT INTO evidence_batch_items(batch_id,evidence_id,ordinal) VALUES(?,?,?) ON CONFLICT(batch_id,ordinal) DO UPDATE SET evidence_id=excluded.evidence_id",
                (batch_id, evidence_id, ordinal),
            )
        return evidence_id

    def batch_count(self, batch_id: str) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM evidence_batch_items WHERE batch_id=?", (batch_id,)).fetchone()[0])

    def bind_batch(self, batch_id: str, violation_id: int, target_qq: str) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT evidence_id,ordinal FROM evidence_batch_items WHERE batch_id=? ORDER BY ordinal",
                (batch_id,),
            ).fetchall()
            if not rows:
                raise ValueError("evidence batch is empty or missing")
            for row in rows:
                conn.execute(
                    "INSERT INTO violation_evidence(violation_id,target_qq,evidence_id,ordinal,bound_at) VALUES(?,?,?,?,?) ON CONFLICT(violation_id,evidence_id) DO NOTHING",
                    (violation_id, target_qq, row["evidence_id"], row["ordinal"], _now()),
                )
            conn.execute(
                "UPDATE evidence_batches SET state='bound',violation_id=?,target_qq=? WHERE id=?",
                (violation_id, target_qq, batch_id),
            )

    def mark_batch(self, batch_id: str, state: str) -> None:
        if state not in {"cancelled", "expired", "error"}:
            raise ValueError("invalid terminal evidence state")
        with self._connect() as conn:
            conn.execute(
                "UPDATE evidence_batches SET state=? WHERE id=? AND violation_id IS NULL",
                (state, batch_id),
            )

    def paths_for_violations(self, violation_ids: list[int]) -> dict[int, tuple[Path, ...]]:
        result: dict[int, tuple[Path, ...]] = {value: () for value in violation_ids}
        if not violation_ids:
            return result
        placeholders = ",".join("?" for _ in violation_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT ve.violation_id,ef.relative_path FROM violation_evidence ve JOIN evidence_files ef ON ef.id=ve.evidence_id WHERE ve.violation_id IN ({placeholders}) ORDER BY ve.violation_id,ve.ordinal",
                violation_ids,
            ).fetchall()
        collected: dict[int, list[Path]] = {value: [] for value in violation_ids}
        for row in rows:
            path = self.root / row["relative_path"]
            if path.is_file():
                collected[int(row["violation_id"])].append(path)
        return {key: tuple(paths) for key, paths in collected.items()}

    def queue_binding(self, batch_id: str, violation_id: int, target_qq: str) -> Path:
        return write_binding_queue(self.root, batch_id, violation_id, target_qq)

    def retry_binding_queue(self) -> int:
        completed = 0
        queue = self.root / "bind-queue"
        if not queue.exists():
            return completed
        for path in sorted(queue.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.bind_batch(payload["batch_id"], int(payload["violation_id"]), str(payload["target_qq"]))
                path.unlink()
                completed += 1
            except (OSError, ValueError, KeyError, json.JSONDecodeError, sqlite3.Error):
                continue
        return completed

    def cleanup_transient(self, now: datetime | None = None) -> dict[str, int]:
        current = now or datetime.now()
        removed_parts = 0
        for path in self.root.rglob("*.part"):
            if datetime.fromtimestamp(path.stat().st_mtime) < current - timedelta(hours=1):
                path.unlink()
                removed_parts += 1
        cutoff = _now(current - timedelta(days=7))
        removed_files = 0
        with self._connect() as conn:
            batches = conn.execute(
                "SELECT id FROM evidence_batches WHERE violation_id IS NULL AND state IN ('staging','cancelled','expired','error') AND created_at<?",
                (cutoff,),
            ).fetchall()
            for batch in batches:
                evidence_ids = [
                    int(row[0])
                    for row in conn.execute("SELECT evidence_id FROM evidence_batch_items WHERE batch_id=?", (batch["id"],))
                ]
                conn.execute("DELETE FROM evidence_batch_items WHERE batch_id=?", (batch["id"],))
                conn.execute("DELETE FROM evidence_batches WHERE id=?", (batch["id"],))
                for evidence_id in evidence_ids:
                    bound = conn.execute("SELECT 1 FROM violation_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
                    pending = conn.execute("SELECT 1 FROM evidence_batch_items WHERE evidence_id=?", (evidence_id,)).fetchone()
                    if bound or pending:
                        continue
                    row = conn.execute("SELECT relative_path FROM evidence_files WHERE id=?", (evidence_id,)).fetchone()
                    if row:
                        (self.root / row["relative_path"]).unlink(missing_ok=True)
                        conn.execute("DELETE FROM evidence_files WHERE id=?", (evidence_id,))
                        removed_files += 1
        return {"parts": removed_parts, "files": removed_files}
```

- [ ] **Step 6: Implement bounded, validated downloads**

Create `evidence_capture.py` with:

```python
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from nonebot import logger

from .config import CONFIG
from .evidence_store import EvidenceStore


@dataclass(frozen=True)
class DownloadedImage:
    content: bytes
    mime_type: str


def _default_resolver(host: str) -> list[str]:
    return list({item[4][0] for item in socket.getaddrinfo(host, None)})


def _valid_signature(content: bytes, mime_type: str) -> bool:
    checks = {
        "image/jpeg": content.startswith(b"\xff\xd8\xff"),
        "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/gif": content.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP",
    }
    return checks.get(mime_type, False)


async def download_image(
    url: str,
    *,
    client: httpx.AsyncClient,
    resolver: Callable[[str], list[str]] = _default_resolver,
    max_bytes: int,
) -> DownloadedImage:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("evidence URL must be HTTP(S)")
    for address in resolver(parsed.hostname):
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("evidence URL resolves to a non-public address")
    async with client.stream("GET", url, follow_redirects=False) as response:
        response.raise_for_status()
        mime_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > max_bytes:
                raise ValueError("evidence image exceeds size limit")
    payload = bytes(content)
    if not _valid_signature(payload, mime_type):
        raise ValueError("evidence payload is not a supported image")
    return DownloadedImage(payload, mime_type)


def _segment_type_data(segment: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(segment, dict):
        return str(segment.get("type") or ""), dict(segment.get("data") or {})
    return str(getattr(segment, "type", "")), dict(getattr(segment, "data", {}) or {})


def _image_urls(message: Any) -> list[str]:
    urls: list[str] = []
    for segment in message or []:
        segment_type, data = _segment_type_data(segment)
        url = str(data.get("url") or "").strip()
        if segment_type == "image" and url.startswith(("http://", "https://")):
            urls.append(url)
    return urls


def _reply_message_id(event: Any) -> str | None:
    for segment in getattr(event, "message", []) or []:
        segment_type, data = _segment_type_data(segment)
        if segment_type == "reply":
            value = data.get("id") or data.get("message_id")
            return str(value) if value is not None else None
    return None


async def referenced_image_urls(bot: Any, event: Any) -> tuple[list[str], str | None]:
    reply = getattr(event, "reply", None)
    if reply is not None and getattr(reply, "message", None) is not None:
        source_id = str(getattr(reply, "message_id", "") or _reply_message_id(event) or "") or None
        return _image_urls(reply.message), source_id
    source_id = _reply_message_id(event)
    if not source_id:
        return [], None
    data = await bot.call_api("get_msg", message_id=source_id)
    if not isinstance(data, dict):
        return [], source_id
    return _image_urls(data.get("message") or []), source_id


async def capture_referenced_images(
    bot: Any,
    event: Any,
    store: EvidenceStore,
    *,
    operator_qq: str,
    command_message_id: str,
    client: httpx.AsyncClient | None = None,
    resolver: Callable[[str], list[str]] = _default_resolver,
) -> tuple[str | None, int]:
    urls, source_message_id = await referenced_image_urls(bot, event)
    if not urls or not source_message_id:
        return None, 0
    batch_id = store.create_batch(
        CONFIG.target_group_id,
        operator_qq,
        command_message_id,
    )
    owned_client = client is None
    active_client = client or httpx.AsyncClient(timeout=20.0)
    stored = 0
    try:
        for ordinal, url in enumerate(urls, 1):
            try:
                image = await download_image(
                    url,
                    client=active_client,
                    resolver=resolver,
                    max_bytes=CONFIG.evidence_max_bytes,
                )
                store.add_bytes(
                    batch_id,
                    image.content,
                    image.mime_type,
                    CONFIG.target_group_id,
                    source_message_id,
                    ordinal,
                )
                stored += 1
            except Exception as exc:
                logger.warning(
                    f"证据图片暂存失败 stage=download message_id={source_message_id} error={type(exc).__name__}"
                )
    finally:
        if owned_client:
            await active_client.aclose()
    if not stored:
        store.mark_batch(batch_id, "error")
        return None, 0
    return batch_id, stored
```

- [ ] **Step 7: Run focused tests**

```bash
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest tests.test_evidence_store tests.test_evidence_capture -v
```

Expected: all storage and download cases pass.

- [ ] **Step 8: Commit evidence storage and capture**

```bash
git add plugins/violation_record/config.py plugins/violation_record/evidence_store.py plugins/violation_record/evidence_capture.py tests/test_evidence_store.py tests/test_evidence_capture.py
git diff --cached --check
git commit -m "feat: persist referenced evidence images"
```

### Task 4: Bind Evidence After a Successful Violation Commit

**Files:**
- Modify: `plugins/violation_record/matcher.py`
- Modify: `plugins/violation_record/service.py`
- Modify: `plugins/violation_record/scheduler.py`
- Test: `tests/test_evidence_service.py`

- [ ] **Step 1: Write failing soft/hard and post-commit binding tests**

Create `tests/test_evidence_service.py` with this complete boundary-focused test:

```python
from __future__ import annotations

import unittest
from contextlib import nullcontext
from dataclasses import replace
from unittest.mock import MagicMock, patch

from plugins.violation_record import service


OPERATOR = {"id": 1, "qq_number": "90001", "nickname": "记录员"}
HANDLER = {"id": 1, "qq_number": "90001", "nickname": "记录员"}
MEMBER = {"id": 2, "qq_number": "123456", "qq_nickname": "小明"}


def valid_create_intent(batch_id: str | None = None, count: int = 0) -> dict:
    return {
        "intent": "create_violation",
        "group_area": "蜂巢",
        "target": {"qq_number": "123456", "qq_nickname": "小明"},
        "violation": {
            "time": "2026-07-28 10:00:00",
            "judgement": "刷屏",
            "action": "禁言10分钟",
            "handler_admin_qq": "90001",
            "handler_admin_nickname": "记录员",
            "remark": None,
        },
        "operation": {"confidence": 1.0, "missing_fields": [], "ambiguous_fields": []},
        "_evidence_batch_id": batch_id,
        "_evidence_count": count,
    }


class EvidenceServiceTests(unittest.TestCase):
    def _preview(self, required: bool, batch_id: str | None = None, count: int = 0):
        with (
            patch.object(service, "CONFIG", replace(service.CONFIG, evidence_required=required)),
            patch.object(service, "_operator_or_message", return_value=OPERATOR),
            patch.object(service, "_resolve_target_for_write", return_value=("ok", MEMBER)),
            patch.object(service, "_resolve_handler_admin", return_value=("ok", HANDLER)),
            patch.object(service, "connect", return_value=nullcontext(MagicMock())),
            patch.object(service, "_state", return_value={"status": "正常", "locked": 0}),
            patch.object(service, "_set_pending") as set_pending,
        ):
            text = service.preview_create(
                valid_create_intent(batch_id, count),
                "123456789",
                "90001",
                "记录员",
                "m1",
            )
        return text, set_pending

    def test_soft_mode_allows_missing_evidence_and_adds_reminder(self) -> None:
        text, set_pending = self._preview(False)
        self.assertIn("未引用证据图片", text)
        set_pending.assert_called_once()

    def test_hard_mode_rejects_missing_evidence_before_pending(self) -> None:
        text, set_pending = self._preview(True)
        self.assertEqual("请引用至少一张证据图片后重新记录。", text)
        set_pending.assert_not_called()

    def test_binding_failure_keeps_confirmation_success_and_queues_retry(self) -> None:
        store = MagicMock()
        store.bind_batch.side_effect = OSError("fixture failure")
        inserted = service.InsertedViolation(
            detail="小明（123456）\n\n时间：2026-07-28 10:00",
            violation_id=42,
            target_qq="123456",
        )
        with (
            patch.object(service, "_pop_pending", return_value=("create_violation", {"record": {}, "evidence_batch_id": "batch-1"})),
            patch.object(service, "_operator_or_message", return_value=OPERATOR),
            patch.object(service, "connect", return_value=nullcontext(MagicMock())),
            patch.object(service, "_insert_violation", return_value=inserted),
            patch.object(service, "EvidenceStore", return_value=store),
        ):
            text = service.confirm_pending("123456789", "90001", "记录员", "m2")
        self.assertIn("已记录。", text)
        store.queue_binding.assert_called_once_with("batch-1", 42, "123456")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the service tests and verify they fail**

```bash
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest tests.test_evidence_service -v
```

Expected: failures because preview, insert return values, and binding queue are not integrated.

- [ ] **Step 3: Capture evidence only after deterministic intent classification**

In `matcher.py`, immediately after `intent["_raw"] = text`, add:

```python
        if intent.get("intent") == "create_violation":
            try:
                batch_id, evidence_count = await capture_referenced_images(
                    bot,
                    event,
                    EvidenceStore(CONFIG.evidence_database_path, CONFIG.evidence_root),
                    operator_qq=str(event.user_id),
                    command_message_id=str(event.message_id),
                )
            except Exception as exc:
                logger.warning(
                    f"证据采集降级 stage=capture message_id={event.message_id} error={type(exc).__name__}"
                )
                batch_id, evidence_count = None, 0
            intent["_evidence_batch_id"] = batch_id
            intent["_evidence_count"] = evidence_count
```

Import only `capture_referenced_images` and `EvidenceStore`; do not change
`parse_intent()` or its prompt/schema.

- [ ] **Step 4: Carry the batch through preview without changing record fields**

In `preview_create()`, after all existing validation and member/admin resolution,
apply:

```python
    evidence_batch_id = intent.get("_evidence_batch_id")
    evidence_count = int(intent.get("_evidence_count") or 0)
    if CONFIG.evidence_required and not evidence_batch_id:
        return "请引用至少一张证据图片后重新记录。"

    pending_payload = {"record": record, "message_id": message_id}
    if evidence_batch_id:
        pending_payload["evidence_batch_id"] = evidence_batch_id
    _set_pending(group_id, operator_qq, "create_violation", pending_payload)

    evidence_note = (
        f"\n\n已暂存证据图片：{evidence_count} 张。"
        if evidence_batch_id
        else "\n\n未引用证据图片；当前为提醒模式，仍可确认入库。"
    )
    return violation_detail(record, member, handler, operator) + evidence_note + "\n\n请回复“确认”入库，或回复“取消”放弃。"
```

This replaces only the existing pending-operation call and final return at the end of
`preview_create()`.

- [ ] **Step 5: Return the inserted ID without changing insert semantics**

Add:

```python
@dataclass(frozen=True)
class InsertedViolation:
    detail: str
    violation_id: int
    target_qq: str
```

Import `dataclass`. In `_insert_violation()`, assign the existing INSERT cursor,
derive `violation_id = int(cursor.lastrowid)`, and return:

```python
    return InsertedViolation(
        detail=violation_detail(record, member, _admin(conn, record["handler_admin_id"]), _admin(conn, record["recorder_admin_id"])),
        violation_id=violation_id,
        target_qq=str(member["qq_number"]),
    )
```

Do not change the INSERT columns, state update, count synchronization, or `_log()` call.

- [ ] **Step 6: Bind only after the business transaction exits**

Restructure only the `create_violation` branch of `confirm_pending()`:

```python
    if operation_type == "create_violation":
        with connect() as conn:
            inserted = _insert_violation(
                conn,
                payload["record"],
                operator,
                payload.get("message_id") or message_id,
            )
        batch_id = payload.get("evidence_batch_id")
        if batch_id:
            try:
                store = EvidenceStore(CONFIG.evidence_database_path, CONFIG.evidence_root)
                try:
                    store.bind_batch(batch_id, inserted.violation_id, inserted.target_qq)
                except Exception as exc:
                    try:
                        store.queue_binding(batch_id, inserted.violation_id, inserted.target_qq)
                    except Exception as queue_exc:
                        logger.warning(
                            f"证据绑定队列写入失败 stage=queue batch={batch_id} record={inserted.violation_id} error={type(queue_exc).__name__}"
                        )
                    logger.warning(
                        f"证据绑定延后 stage=bind batch={batch_id} record={inserted.violation_id} error={type(exc).__name__}"
                    )
            except Exception as exc:
                try:
                    write_binding_queue(
                        CONFIG.evidence_root,
                        batch_id,
                        inserted.violation_id,
                        inserted.target_qq,
                    )
                except Exception as queue_exc:
                    logger.warning(
                        f"证据应急队列写入失败 stage=queue batch={batch_id} record={inserted.violation_id} error={type(queue_exc).__name__}"
                    )
                logger.warning(
                    f"证据存储不可用 stage=store batch={batch_id} record={inserted.violation_id} error={type(exc).__name__}"
                )
        return inserted.detail.replace("\n\n时间", "\n\n已记录。\n\n时间", 1)
```

Import `write_binding_queue` with `EvidenceStore`. Keep all other
pending-operation branches unchanged.

Change `_pop_pending()` so an expired row returns its decoded payload:

```python
        payload = json.loads(row["payload_json"])
        if datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S") < datetime.now():
            return "expired", payload
        return row["operation_type"], payload
```

On expired or cancelled create operations, call `mark_batch(batch_id, "expired")`
or `mark_batch(batch_id, "cancelled")` when the payload contains a batch ID.

- [ ] **Step 7: Add binding retry and transient cleanup to the existing loop**

At the bottom of each scheduler iteration, call:

```python
            evidence_store = EvidenceStore(CONFIG.evidence_database_path, CONFIG.evidence_root)
            evidence_store.retry_binding_queue()
            evidence_store.cleanup_transient()
```

These functions must not send group messages and must catch per-item errors so one
bad queue file does not stop later retries.

- [ ] **Step 8: Run service, query, and evidence tests**

```bash
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest tests.test_evidence_service tests.test_evidence_store tests.test_query_contract -v
```

Expected: all tests pass and the original query contract still passes unchanged.

- [ ] **Step 9: Commit post-commit evidence binding**

```bash
git add plugins/violation_record/matcher.py plugins/violation_record/service.py plugins/violation_record/scheduler.py tests/test_evidence_service.py
git diff --cached --check
git commit -m "feat: bind evidence after violation commit"
```

### Task 5: Add Structured Query Replies and Mixed-Message Delivery

**Files:**
- Create: `plugins/violation_record/reply_models.py`
- Modify: `plugins/violation_record/service.py`
- Modify: `plugins/violation_record/matcher.py`
- Modify: `tests/test_query_contract.py`
- Create: `tests/test_reply_delivery.py`

- [ ] **Step 1: Write failing reply-model and old-record tests**

Create `tests/test_reply_delivery.py`:

```python
from pathlib import Path
import unittest

from plugins.violation_record.reply_models import RecordMessage, StructuredReply


class ReplyModelTests(unittest.TestCase):
    def test_record_keeps_all_images_in_order(self) -> None:
        reply = StructuredReply(
            records=(RecordMessage("1. record", (Path("a.jpg"), Path("b.png"))),)
        )
        self.assertEqual((Path("a.jpg"), Path("b.png")), reply.records[0].images)

    def test_old_record_has_empty_image_tuple(self) -> None:
        self.assertEqual((), RecordMessage("1. old").images)
```

Update `tests/test_query_contract.py` with a helper that flattens a
`StructuredReply` by joining each record's text with `\n`. The flattened text must
equal the original golden string exactly. Add a test that patches
`service.EvidenceStore` to raise `sqlite3.DatabaseError("fixture")`; the same
flattened member-query text must still match the golden string and every record's
`images` tuple must be empty.

- [ ] **Step 2: Run the tests and verify the reply-model import fails**

```bash
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest tests.test_reply_delivery tests.test_query_contract -v
```

Expected: import failure for `reply_models`.

- [ ] **Step 3: Implement the immutable reply envelope**

Create `reply_models.py`:

```python
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RecordMessage:
    text: str
    images: tuple[Path, ...] = ()


@dataclass(frozen=True)
class StructuredReply:
    records: tuple[RecordMessage, ...]
```

Import `StructuredReply` and `RecordMessage` in `service.py`, import `CONFIG` and
`logger`, and change only these return annotations:

```python
async def handle_intent(intent: dict[str, Any], group_id: str, operator_qq: str, operator_nickname: str | None, message_id: str | None = None) -> str | StructuredReply:
def query_area_records(intent: dict[str, Any], operator_qq: str, operator_nickname: str | None, message_id: str | None) -> str | StructuredReply:
def query_member(intent: dict[str, Any], operator_qq: str, operator_nickname: str | None, recent: bool, message_id: str | None) -> str | StructuredReply:
```

The parameters remain exactly as they are today; only return annotations change.

- [ ] **Step 4: Structure query output after the existing SQL completes**

In both query functions, leave the SQL blocks untouched. Replace only text
assembly after `rows` has been fetched. Resolve all evidence in one sidecar query
with a fail-open display boundary:

```python
    violation_ids = [int(row["id"]) for row in rows]
    try:
        evidence = EvidenceStore(
            CONFIG.evidence_database_path, CONFIG.evidence_root
        ).paths_for_violations(violation_ids)
    except Exception as exc:
        logger.warning(
            f"证据查询降级 stage=query error={type(exc).__name__}"
        )
        evidence = {violation_id: () for violation_id in violation_ids}
```

For member queries, build the same header and line strings already used:

```python
    header = [format_member(member), "", f"当前次数：{current}", f"状态：{state['status']}", "", "具体记录：", ""]
    if not rows:
        return "\n".join([*header, "无记录。"])
    records = []
    for index, row in enumerate(rows, 1):
        line = f"{index}. {display_time(row['violation_time'])}，{row['judgement']}，{row['action']}"
        text = "\n".join([*header, line]) if index == 1 else line
        records.append(RecordMessage(text, evidence[int(row["id"])]))
    return StructuredReply(tuple(records))
```

For area queries, use the current area header and record line. If `total >
len(rows)`, append the current limited-result/export footer to the final record text.
Do not change the limit, WHERE clause, parameters, count query, or ORDER BY.

- [ ] **Step 5: Send one mixed OneBot message per record**

Add to `matcher.py`:

```python
async def _send_structured_reply(bot: Bot, group_id: int, reply: StructuredReply) -> None:
    for record in reply.records:
        message = Message(record.text)
        existing_images = [path for path in record.images if path.is_file()]
        for path in existing_images:
            message += MessageSegment.image(file=f"file://{path}")
        try:
            await bot.send_group_msg(group_id=group_id, message=message)
        except Exception as exc:
            logger.warning(
                f"证据混合消息发送失败 stage=query group={group_id} error={type(exc).__name__}"
            )
            await bot.send_group_msg(group_id=group_id, message=record.text)
            for path in existing_images:
                try:
                    await bot.send_group_msg(
                        group_id=group_id,
                        message=MessageSegment.image(file=f"file://{path}"),
                    )
                except Exception as image_exc:
                    logger.warning(
                        f"单张证据发送失败 stage=query group={group_id} error={type(image_exc).__name__}"
                    )
```

Import `Message`, `MessageSegment`, and `StructuredReply`. Before
`_upload_export_files`, branch:

```python
    if isinstance(reply, StructuredReply):
        await _send_structured_reply(bot, int(event.group_id), reply)
        await matcher.finish()
```

String replies and export uploads continue through the existing path unchanged.

- [ ] **Step 6: Run reply and golden contract tests**

```bash
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest tests.test_reply_delivery tests.test_query_contract -v
```

Expected: all tests pass; flattening the structured messages reproduces the exact
pre-change text and order.

- [ ] **Step 7: Commit the rendering layer**

```bash
git add plugins/violation_record/reply_models.py plugins/violation_record/service.py plugins/violation_record/matcher.py tests/test_reply_delivery.py tests/test_query_contract.py
git diff --cached --check
git commit -m "feat: attach evidence to query records"
```

### Task 6: Enforce the Mute and Logging Boundaries

**Files:**
- Modify: `plugins/violation_record/config.py`
- Modify: `plugins/violation_record/moderation.py`
- Modify: `plugins/violation_record/matcher.py`
- Modify: `.env.example`
- Test: `tests/test_mute_switch.py`
- Test: `tests/test_chat_archive.py`

- [ ] **Step 1: Write a failing no-OneBot-call mute test**

Create `tests/test_mute_switch.py` with `unittest.IsolatedAsyncioTestCase`. Patch
`moderation.CONFIG.mute_enabled` to `False`, pass an `AsyncMock` bot, call
`handle_mute_intent()`, and assert:

```python
self.assertEqual("禁言功能未启用。", result)
bot.set_group_ban.assert_not_awaited()
bot.call_api.assert_not_awaited()
```

- [ ] **Step 2: Run the mute test and verify it fails**

```bash
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest tests.test_mute_switch -v
```

Expected: failure because `mute_enabled` and the early return do not exist.

- [ ] **Step 3: Add and enforce the mute switch before validation or API access**

Add to `AppConfig`:

```python
    mute_enabled: bool = _bool_env("MUTE_ENABLED", False)
```

At the first line of `handle_mute_intent()` add:

```python
    if not CONFIG.mute_enabled:
        return "禁言功能未启用。"
```

Import `CONFIG` into `moderation.py`. Add `MUTE_ENABLED=false` to `.env.example`.

- [ ] **Step 4: Remove custom chat-content logging**

Delete the `logger.info` block in the business matcher that includes
`event.get_plaintext()`. Keep only sanitized warnings that contain stage, group,
message ID, and exception class. Do not add another message-body logger.

Change `only_allowed_group()` to the exact comparison:

```python
async def only_allowed_group(event: Event) -> bool:
    return (
        isinstance(event, GroupMessageEvent)
        and int(event.group_id) == CONFIG.target_group_id
    )
```

Extend `tests/test_chat_archive.py` with a fake non-target event and mocks for
`parse_intent`, `grant_admin`, archive insert, and media capture. Assert none are
called after the exact-group rule rejects it.

- [ ] **Step 5: Run boundary tests**

```bash
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest tests.test_mute_switch tests.test_chat_archive -v
```

Expected: all tests pass and the bot mock has no moderation API calls.

- [ ] **Step 6: Commit the boundary controls**

```bash
git add .env.example plugins/violation_record/config.py plugins/violation_record/moderation.py plugins/violation_record/matcher.py tests/test_mute_switch.py tests/test_chat_archive.py
git diff --cached --check
git commit -m "feat: enforce target group and mute boundaries"
```

### Task 7: Document, Validate, and Stage Runtime Configuration

**Files:**
- Modify: `README.md`
- Runtime modify during deployment: `.env`

- [ ] **Step 1: Document exact archive/evidence behavior**

Update README with these configuration keys and defaults:

```dotenv
LOG_LEVEL=WARNING
EVIDENCE_REQUIRED=false
EVIDENCE_MAX_BYTES=20971520
MUTE_ENABLED=false
```

Document that only the single `TARGET_GROUP_ID` is processed; all other groups
are dropped after ingress; target messages are archived; only images referenced by
new violation commands are downloaded; each query record is sent with all mapped
images; old records without evidence remain valid.

- [ ] **Step 2: Run the complete offline suite**

```bash
TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest discover -s tests -v
TARGET_GROUP_ID=123456789 .venv/bin/python -m compileall -q bot.py plugins scripts tests
bash -n scripts/backup_db.sh scripts/start_bot.sh scripts/start_napcat.sh
.venv/bin/pip check
```

Expected: all tests and static checks pass.

- [ ] **Step 3: Prove no protected module changed**

```bash
BASELINE=$(git log --format=%H --grep='^chore: add sanitized application baseline$' -n 1)
test -n "$BASELINE"
git diff "$BASELINE"..HEAD -- plugins/violation_record/ai_router.py plugins/violation_record/schemas.py plugins/violation_record/validators.py plugins/violation_record/member_resolver.py
```

Expected: empty output. Review `service.py` and confirm only pending evidence,
insert return metadata, and post-query rendering changed; query SQL is byte-for-byte
unchanged.

- [ ] **Step 4: Run public-tree and history scans**

```bash
TARGET_GROUP_ID=123456789 .venv/bin/python scripts/check_public_tree.py --history
git diff --check
```

Expected: `public source scan: PASS` and no diff errors.

- [ ] **Step 5: Commit documentation**

```bash
git add README.md
git commit -m "docs: describe target archive and evidence"
```

## Plan 2 Completion Gate

Proceed only when the entire offline suite passes, the query SQL and protected
modules are unchanged, public scans pass, and no production service has yet been
restarted. At this point code is ready but not live until Plan 3 performs a
snapshot, controlled restart, and acceptance checks.
