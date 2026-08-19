# 群友永久记忆与滚动摘要 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将群友特性和历史昵称改为永不截断的追加式账本，并用滚动摘要向聊天 AI 提供有界上下文。

**Architecture:** SQLite 新增原始特性、昵称历史和摘要状态三张表；旧 `member_memories` 字段继续保存最近 8 条兼容视图。摘要生成独立于原始写入，只有成功后才推进游标；随机聊天读取摘要加有界未摘要事实，本地 JSON 镜像展示完整历史。

**Tech Stack:** Python 3.10、SQLite、NoneBot2、httpx、unittest

---

### Task 1: 追加式原始记忆与昵称账本

**Files:**
- Modify: `plugins/member_memory/store.py`
- Modify: `tests/test_member_memory.py`

- [ ] **Step 1: 将旧上限测试改为永久保留的失败测试**

在 `MemberMemoryStoreTests` 中将候选测试改为 10 条有效特性全部写入，并新增 10 次改名测试：

```python
def test_candidates_keep_every_valid_fact_beyond_legacy_limit(self):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "member_memory"
        db = Path(directory) / "chat.db"
        context = [ContextMessage("小明", "我喜欢火锅", message_id="m1", user_id="7")]
        candidates = [
            {
                "user_id": "7",
                "trait": f"爱好{i}",
                "evidence_message_id": "m1",
                "quote": "我喜欢火锅",
            }
            for i in range(10)
        ]
        self.assertEqual(10, apply_candidates(db, root, group_id=123, context=context, candidates=candidates))
        self.assertEqual(0, apply_candidates(db, root, group_id=123, context=context, candidates=candidates))
        profile = load_profiles(db, group_id=123, user_ids=["7"])[0]
        self.assertEqual([f"爱好{i}" for i in range(10)], [item.text for item in profile.traits])

def test_identity_keeps_all_historical_aliases(self):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "member_memory"
        db = Path(directory) / "chat.db"
        for index in range(10):
            remember_identity(db, root, group_id=123, user_id="7", nickname=f"名字{index}")
        profile = load_profiles(db, group_id=123, user_ids=["7"])[0]
        self.assertEqual("名字9", profile.nickname)
        self.assertEqual(tuple(f"名字{i}" for i in range(9)), profile.aliases)
```

- [ ] **Step 2: 运行存储测试并确认 RED**

Run: `/opt/qq-violation-bot/.venv/bin/python -m unittest tests.test_member_memory.MemberMemoryStoreTests -v`

Expected: 10 条特性只剩 8 条，10 个历史昵称只剩 8 个。

- [ ] **Step 3: 新增账本 schema 和扩展数据类型**

在 `store.py` 增加：

```python
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
PROMPT_ALIAS_LIMIT = 5
PROMPT_UNSUMMARIZED_LIMIT = 8

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
```

所有连接通过 `_ensure_schema(conn)` 同时执行旧 schema 与新 schema。

- [ ] **Step 4: 改为账本写入并保留旧字段兼容视图**

实现以下行为：

```python
def _append_alias(conn, group_id, user_id, alias, seen_at):
    conn.execute(
        "INSERT OR IGNORE INTO member_memory_aliases(group_id,user_id,alias,first_seen_at) VALUES(?,?,?,?)",
        (group_id, user_id, alias, seen_at),
    )

def _append_fact(conn, group_id, user_id, trait, evidence_id, created_at):
    cursor = conn.execute(
        "INSERT OR IGNORE INTO member_memory_facts(group_id,user_id,trait,evidence_message_id,created_at) VALUES(?,?,?,?,?)",
        (group_id, user_id, trait, evidence_id, created_at),
    )
    return cursor.rowcount == 1
```

`remember_identity` 在昵称变化时将旧昵称追加到账本；`apply_candidates` 移除 `accepted_per_user >= MAX_TRAITS` 和 `traits[-MAX_TRAITS:]`，将每条有效事实追加到账本。将 `_write_mirror` 改为 `_write_mirror(path, root, group_id, user_id)`，由它重新读取账本完整历史；每次写入后将完整历史写入镜像，并将最近 8 条同步到旧 `traits_json` / `aliases_json`。

- [ ] **Step 5: 运行存储测试并确认 GREEN**

Run: `/opt/qq-violation-bot/.venv/bin/python -m unittest tests.test_member_memory.MemberMemoryStoreTests -v`

Expected: 全部通过，第 9、10 条特性和昵称仍存在。

- [ ] **Step 6: 提交账本实现**

```bash
git add plugins/member_memory/store.py tests/test_member_memory.py
git commit -m "feat: preserve complete member memory history"
```

### Task 2: 滚动摘要生成与原子游标

**Files:**
- Modify: `plugins/member_memory/ai.py`
- Modify: `plugins/member_memory/store.py`
- Create: `plugins/member_memory/summary.py`
- Create: `tests/test_member_memory_summary.py`

- [ ] **Step 1: 写摘要阈值、成功和失败测试**

```python
def seed_facts(path: Path, root: Path, *, count: int) -> None:
    context = [ContextMessage("小明", "我喜欢火锅", message_id="m1", user_id="7")]
    candidates = [
        {
            "user_id": "7",
            "trait": f"特性{index}",
            "evidence_message_id": "m1",
            "quote": "我喜欢火锅",
        }
        for index in range(count)
    ]
    apply_candidates(path, root, group_id=123, context=context, candidates=candidates)

class MemberMemorySummaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = Path(self.temporary.name) / "chat.db"
        self.root = Path(self.temporary.name) / "member_memory"

    async def test_five_pending_facts_refresh_summary_and_advance_cursor(self):
        seed_facts(self.db, self.root, count=5)
        with patch("plugins.member_memory.summary.generate_memory_summary", AsyncMock(return_value="喜欢火锅，也常养花")):
            self.assertTrue(await refresh_member_summary(self.db, self.root, group_id=123, user_id="7"))
        profile = load_profiles(self.db, group_id=123, user_ids=["7"])[0]
        self.assertEqual("喜欢火锅，也常养花", profile.summary)
        self.assertEqual(5, profile.summary_through_fact_id)

    async def test_summary_failure_keeps_cursor_and_raw_facts(self):
        seed_facts(self.db, self.root, count=5)
        with patch("plugins.member_memory.summary.generate_memory_summary", AsyncMock(return_value=None)):
            self.assertFalse(await refresh_member_summary(self.db, self.root, group_id=123, user_id="7"))
        profile = load_profiles(self.db, group_id=123, user_ids=["7"])[0]
        self.assertEqual("", profile.summary)
        self.assertEqual(0, profile.summary_through_fact_id)
        self.assertEqual(5, len(profile.traits))

    async def test_four_pending_facts_do_not_call_ai(self):
        seed_facts(self.db, self.root, count=4)
        generate = AsyncMock(return_value="不应生成")
        with patch("plugins.member_memory.summary.generate_memory_summary", generate):
            self.assertFalse(await refresh_member_summary(self.db, self.root, group_id=123, user_id="7"))
        generate.assert_not_awaited()

    async def test_twenty_five_pending_facts_are_summarized_in_two_batches(self):
        seed_facts(self.db, self.root, count=25)
        generate = AsyncMock(side_effect=["第一批摘要", "最终摘要"])
        with patch("plugins.member_memory.summary.generate_memory_summary", generate):
            self.assertTrue(await refresh_member_summary(self.db, self.root, group_id=123, user_id="7"))
        self.assertEqual(2, generate.await_count)
        profile = load_profiles(self.db, group_id=123, user_ids=["7"])[0]
        self.assertEqual("最终摘要", profile.summary)
        self.assertEqual(25, profile.summary_through_fact_id)

    async def test_stale_cursor_cannot_overwrite_newer_summary(self):
        seed_facts(self.db, self.root, count=5)
        self.assertTrue(commit_summary(
            self.db, self.root, group_id=123, user_id="7",
            previous_through_id=0, through_fact_id=5, summary="新摘要",
        ))
        self.assertFalse(commit_summary(
            self.db, self.root, group_id=123, user_id="7",
            previous_through_id=0, through_fact_id=4, summary="过期摘要",
        ))
```

- [ ] **Step 2: 运行摘要测试并确认 RED**

Run: `/opt/qq-violation-bot/.venv/bin/python -m unittest tests.test_member_memory_summary -v`

Expected: `plugins.member_memory.summary` 不存在。

- [ ] **Step 3: 实现保守摘要 AI**

在 `ai.py` 增加：

```python
async def generate_memory_summary(existing: str, facts: Sequence[MemoryTrait]) -> str | None:
    if not CONFIG.ai_api_key or not facts:
        return None
    payload = {
        "model": CONFIG.ai_model,
        "messages": [
            {"role": "system", "content": "将已有摘要和新增的本人记忆合并为不超过300字的中文摘要。只保留输入中的明确非敏感事实，不推测、不扩写、不评价，只输出摘要正文。"},
            {"role": "user", "content": "已有摘要：\n" + (existing or "（无）") + "\n新增记忆：\n" + "\n".join(f"- {item.text}" for item in facts)},
        ],
        "temperature": 0.1,
    }
    try:
        async with httpx.AsyncClient(timeout=CONFIG.ai_timeout) as client:
            response = await client.post(
                f"{CONFIG.ai_base_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {CONFIG.ai_api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            text = str(response.json()["choices"][0]["message"]["content"]).strip()
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
        return None
    return text if text and len(text) <= 300 else None
```

- [ ] **Step 4: 实现待摘要查询和 CAS 提交**

在 `store.py` 增加：

```python
@dataclass(frozen=True)
class SummaryWork:
    summary: str
    previous_through_id: int
    facts: tuple[MemoryTrait, ...]

def pending_summary_batch(path, *, group_id, user_id, threshold=5, limit=20):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        state = conn.execute(
            "SELECT summary_text,through_fact_id FROM member_memory_summaries WHERE group_id=? AND user_id=?",
            (group_id, user_id),
        ).fetchone()
        summary = str(state["summary_text"]) if state else ""
        through = int(state["through_fact_id"]) if state else 0
        pending_count = conn.execute(
            "SELECT count(*) FROM member_memory_facts WHERE group_id=? AND user_id=? AND id>?",
            (group_id, user_id, through),
        ).fetchone()[0]
        if pending_count < threshold:
            return None
        rows = conn.execute(
            "SELECT id,trait,evidence_message_id,created_at FROM member_memory_facts "
            "WHERE group_id=? AND user_id=? AND id>? ORDER BY id LIMIT ?",
            (group_id, user_id, through, limit),
        ).fetchall()
    facts = tuple(MemoryTrait(row["trait"], row["evidence_message_id"], row["created_at"], row["id"]) for row in rows)
    return SummaryWork(summary, through, facts)

def commit_summary(path, root, *, group_id, user_id, previous_through_id, through_fact_id, summary):
    with sqlite3.connect(path) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT through_fact_id FROM member_memory_summaries WHERE group_id=? AND user_id=?",
            (group_id, user_id),
        ).fetchone()
        current = int(row[0]) if row else 0
        if current != previous_through_id:
            conn.rollback()
            return False
        conn.execute(
            "INSERT INTO member_memory_summaries(group_id,user_id,summary_text,through_fact_id,updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(group_id,user_id) DO UPDATE SET "
            "summary_text=excluded.summary_text,through_fact_id=excluded.through_fact_id,updated_at=excluded.updated_at",
            (group_id, user_id, summary, through_fact_id, _now()),
        )
        conn.commit()
    _write_mirror(path, root, group_id, user_id)
    return True
```

在 `summary.py` 实现：

```python
SUMMARY_THRESHOLD = 5
SUMMARY_BATCH_LIMIT = 20

async def refresh_member_summary(path: Path, root: Path, *, group_id: int, user_id: str) -> bool:
    refreshed = False
    while True:
        work = pending_summary_batch(
            path, group_id=group_id, user_id=user_id,
            threshold=SUMMARY_THRESHOLD, limit=SUMMARY_BATCH_LIMIT,
        )
        if work is None:
            return refreshed
        text = await generate_memory_summary(work.summary, work.facts)
        if text is None:
            return refreshed
        if not commit_summary(
            path,
            root,
            group_id=group_id,
            user_id=user_id,
            previous_through_id=work.previous_through_id,
            through_fact_id=work.facts[-1].fact_id,
            summary=text,
        ):
            return refreshed
        refreshed = True
```

- [ ] **Step 5: 运行摘要与存储测试并确认 GREEN**

Run: `/opt/qq-violation-bot/.venv/bin/python -m unittest tests.test_member_memory_summary tests.test_member_memory -v`

Expected: 全部通过。

- [ ] **Step 6: 提交摘要核心**

```bash
git add plugins/member_memory/ai.py plugins/member_memory/store.py plugins/member_memory/summary.py tests/test_member_memory_summary.py
git commit -m "feat: add rolling member memory summaries"
```

### Task 3: 接入实时提炼与有界聊天读取

**Files:**
- Modify: `plugins/violation_record/config.py`
- Modify: `.env.example`
- Modify: `plugins/member_memory/matcher.py`
- Modify: `plugins/random_chat/matcher.py`
- Modify: `plugins/random_chat/ai.py`
- Modify: `tests/test_member_memory.py`
- Modify: `tests/test_random_chat.py`

- [ ] **Step 1: 写实时摘要和提示词边界失败测试**

```python
async def test_analysis_refreshes_summary_after_new_facts(self):
    context = [ContextMessage("小明", "我喜欢火锅", message_id="m1", user_id="7")]
    candidates = [{"user_id": "7", "trait": "喜欢火锅"}]
    config = SimpleNamespace(
        chat_archive_path=Path("/tmp/chat.db"),
        member_memory_root=Path("/tmp/member-memory"),
        bot_self_id="999",
        member_memory_summary_enabled=True,
    )
    with patch.object(memory_matcher, "CONFIG", config), patch.object(
        memory_matcher, "recent_text_context", return_value=context
    ), patch.object(
        memory_matcher, "extract_memory_candidates", AsyncMock(return_value=candidates)
    ), patch.object(
        memory_matcher, "apply_candidates", return_value=1
    ), patch.object(
        memory_matcher, "refresh_member_summary", AsyncMock(return_value=True)
    ) as refresh:
        await memory_matcher.analyze_member_memory(123, "7", 2000)
    refresh.assert_awaited_once_with(
        config.chat_archive_path, config.member_memory_root, group_id=123, user_id="7"
    )

def test_compact_profile_contains_summary_and_only_bounded_pending_facts(self):
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "chat.db"
        root = Path(directory) / "member_memory"
        seed_facts(db, root, count=10)
        for index in range(10):
            remember_identity(db, root, group_id=123, user_id="7", nickname=f"名字{index}")
        self.assertTrue(commit_summary(
            db, root, group_id=123, user_id="7", previous_through_id=0,
            through_fact_id=2, summary="长期喜欢植物",
        ))
        profile = load_profiles(db, group_id=123, user_ids=["7"], compact=True)[0]
        self.assertEqual("长期喜欢植物", profile.summary)
        self.assertEqual(8, len(profile.traits))
        self.assertEqual(5, len(profile.aliases))
```

在 `test_random_chat.py` 构造带 `summary="长期喜欢植物"` 的 `MemberProfile`，断言系统用户内容包含 `记忆摘要:长期喜欢植物`，且最近特性仍以 `新增特性:` 输出。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `/opt/qq-violation-bot/.venv/bin/python -m unittest tests.test_member_memory tests.test_random_chat -v`

Expected: 配置项、`compact` 参数和摘要刷新调用尚不存在。

- [ ] **Step 3: 接入开关和实时摘要**

在 `AppConfig` 和 `.env.example` 增加：

```python
member_memory_summary_enabled: bool = _bool_env("MEMBER_MEMORY_SUMMARY_ENABLED", False)
```

```dotenv
MEMBER_MEMORY_SUMMARY_ENABLED=false
```

`analyze_member_memory` 保存候选后，仅在 `applied > 0` 且开关启用时调用 `refresh_member_summary`；异常继续由现有回调边界捕获，原始事实已经提交且不回滚。

- [ ] **Step 4: 实现 compact 读取和摘要格式**

`load_profiles(..., compact=False)` 保持默认返回完整账本；`compact=True` 返回当前摘要、最近 5 个历史昵称和摘要游标之后最近 8 条事实。`random_chat.matcher` 使用 `compact=True`。

将 `_format_profile` 改为：

```python
def _format_profile(profile: MemberProfile) -> str:
    details = []
    if profile.aliases:
        details.append("旧称:" + "、".join(profile.aliases))
    if profile.summary:
        details.append("记忆摘要:" + profile.summary)
    if profile.traits:
        details.append("新增特性:" + "；".join(item.text for item in profile.traits))
    return f"{profile.nickname}[QQ:{profile.user_id}] " + ("；".join(details) or "无稳定特性")
```

- [ ] **Step 5: 运行相关测试并确认 GREEN**

Run: `/opt/qq-violation-bot/.venv/bin/python -m unittest tests.test_member_memory tests.test_member_memory_summary tests.test_random_chat tests.test_random_chat_context tests.test_violation_chat_fallback -v`

Expected: 全部通过，业务路由优先级不变。

- [ ] **Step 6: 提交集成**

```bash
git add .env.example plugins/violation_record/config.py plugins/member_memory/matcher.py plugins/random_chat/matcher.py plugins/random_chat/ai.py tests/test_member_memory.py tests/test_random_chat.py
git commit -m "feat: use compact summaries in member-aware chat"
```

### Task 4: 幂等迁移与完整本地镜像

**Files:**
- Modify: `plugins/member_memory/store.py`
- Create: `scripts/migrate_member_memory_v2.py`
- Create: `tests/test_member_memory_migration.py`
- Modify: `tests/test_member_memory.py`

- [ ] **Step 1: 写迁移 dry-run、apply 和镜像失败测试**

```python
def table_exists(path: Path, name: str) -> bool:
    with sqlite3.connect(path) as conn:
        return bool(conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type=? AND name=?", ("table", name)
        ).fetchone()[0])

def count_facts(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute("SELECT count(*) FROM member_memory_facts").fetchone()[0])

def seed_additional_facts(path: Path, root: Path, *, count: int) -> None:
    context = [ContextMessage("当前名", "我喜欢火锅", message_id="new-message", user_id="7")]
    candidates = [
        {
            "user_id": "7",
            "trait": f"新增特性{index}",
            "evidence_message_id": "new-message",
            "quote": "我喜欢火锅",
        }
        for index in range(count)
    ]
    apply_candidates(path, root, group_id=123, context=context, candidates=candidates)

class MemberMemoryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = Path(self.temporary.name) / "chat.db"
        self.root = Path(self.temporary.name) / "member_memory"
        traits = [
            {"text": f"特性{index}", "evidence_message_id": f"m{index}", "updated_at": "2026-08-19 10:00:00"}
            for index in range(8)
        ]
        with sqlite3.connect(self.db) as conn:
            conn.executescript(MEMORY_SCHEMA)
            conn.execute(
                "INSERT INTO member_memories(group_id,user_id,nickname,aliases_json,traits_json,updated_at) VALUES(?,?,?,?,?,?)",
                (123, "7", "当前名", json.dumps(["旧名1", "旧名2"], ensure_ascii=False), json.dumps(traits, ensure_ascii=False), "2026-08-19 10:00:00"),
            )

    def test_dry_run_reports_without_writing(self):
        report = migrate_legacy_memory(self.db, self.root, apply=False)
        self.assertEqual(8, report.source_facts)
        self.assertEqual(2, report.source_aliases)
        self.assertEqual(0, report.inserted_facts)
        self.assertFalse(table_exists(self.db, "member_memory_facts"))

    def test_apply_is_idempotent_and_preserves_counts(self):
        first = migrate_legacy_memory(self.db, self.root, apply=True)
        second = migrate_legacy_memory(self.db, self.root, apply=True)
        self.assertEqual(8, first.inserted_facts)
        self.assertEqual(2, first.inserted_aliases)
        self.assertEqual(0, second.inserted_facts)
        self.assertEqual(0, second.inserted_aliases)
        self.assertEqual(8, count_facts(self.db))

    def test_mirror_contains_complete_history_and_summary(self):
        migrate_legacy_memory(self.db, self.root, apply=True)
        seed_additional_facts(self.db, self.root, count=10)
        for index in range(10):
            remember_identity(self.db, self.root, group_id=123, user_id="7", nickname=f"名字{index}")
        commit_summary(
            self.db, self.root, group_id=123, user_id="7",
            previous_through_id=0, through_fact_id=8, summary="长期喜欢植物",
        )
        payload = json.loads((self.root / "123" / "7.json").read_text())
        self.assertEqual(18, len(payload["traits"]))
        self.assertGreaterEqual(len(payload["aliases"]), 11)
        self.assertEqual("长期喜欢植物", payload["summary"])
```

- [ ] **Step 2: 运行迁移测试并确认 RED**

Run: `/opt/qq-violation-bot/.venv/bin/python -m unittest tests.test_member_memory_migration -v`

Expected: 迁移函数和脚本不存在。

- [ ] **Step 3: 实现只读预演和幂等迁移**

```python
@dataclass(frozen=True)
class MemoryMigrationReport:
    profiles: int
    source_facts: int
    source_aliases: int
    inserted_facts: int
    inserted_aliases: int

def migrate_legacy_memory(path: Path, root: Path, *, apply: bool) -> MemoryMigrationReport:
    if not path.is_file():
        return MemoryMigrationReport(0, 0, 0, 0, 0)
    with sqlite3.connect(path) as conn:
        exists = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type=? AND name=?",
            ("table", "member_memories"),
        ).fetchone()[0]
        if not exists:
            return MemoryMigrationReport(0, 0, 0, 0, 0)
        rows = conn.execute(
            "SELECT group_id,user_id,nickname,aliases_json,traits_json,updated_at FROM member_memories"
        ).fetchall()
        profiles = [_decode_profile(row) for row in rows]
        source_facts = sum(len(profile.traits) for profile in profiles)
        source_aliases = sum(len(profile.aliases) for profile in profiles)
        if not apply:
            return MemoryMigrationReport(len(profiles), source_facts, source_aliases, 0, 0)
        _ensure_schema(conn)
        inserted_facts = 0
        inserted_aliases = 0
        for profile in profiles:
            for trait in profile.traits:
                inserted_facts += int(_append_fact(
                    conn, profile.group_id, profile.user_id, trait.text,
                    trait.evidence_message_id, trait.updated_at or profile.updated_at,
                ))
            for alias in profile.aliases:
                before = conn.total_changes
                _append_alias(conn, profile.group_id, profile.user_id, alias, profile.updated_at)
                inserted_aliases += int(conn.total_changes > before)
        conn.commit()
    for profile in profiles:
        _write_mirror(path, root, profile.group_id, profile.user_id)
    return MemoryMigrationReport(
        len(profiles), source_facts, source_aliases, inserted_facts, inserted_aliases
    )
```

脚本使用互斥参数：

```python
mode = parser.add_mutually_exclusive_group(required=True)
mode.add_argument("--dry-run", action="store_true")
mode.add_argument("--apply", action="store_true")
parser.add_argument("--summarize", action="store_true")
parser.add_argument("--database", type=Path, default=PROJECT_DIR / "data" / "chat_archive.db")
parser.add_argument("--mirror-root", type=Path, default=PROJECT_DIR / "data" / "member_memory")
```

脚本启动时执行 `load_dotenv(PROJECT_DIR / ".env")`。`--summarize` 只允许与 `--apply` 同时使用，只处理未摘要事实不少于 5 条的成员，逐成员调用 `refresh_member_summary(database, mirror_root, ...)`；输出仅包含成员数、源数量、插入数量和摘要成功/失败计数，不输出昵称、特性或 QQ号。

- [ ] **Step 4: 扩展镜像为完整历史**

`_write_mirror` 从账本重新读取完整昵称与事实，写入：

```json
{
  "group_id": 123,
  "user_id": "7",
  "nickname": "当前昵称",
  "aliases": ["全部历史昵称"],
  "summary": "滚动摘要",
  "summary_through_fact_id": 12,
  "traits": [{"text": "喜欢火锅", "evidence_message_id": "m1", "updated_at": "2026-08-19 10:00:00", "fact_id": 1}],
  "updated_at": "2026-08-19 10:05:00"
}
```

- [ ] **Step 5: 运行迁移和存储测试并确认 GREEN**

Run: `/opt/qq-violation-bot/.venv/bin/python -m unittest tests.test_member_memory_migration tests.test_member_memory tests.test_member_memory_summary -v`

Expected: 全部通过，第二次迁移插入数为 0。

- [ ] **Step 6: 提交迁移工具**

```bash
git add plugins/member_memory/store.py scripts/migrate_member_memory_v2.py tests/test_member_memory.py tests/test_member_memory_migration.py
git commit -m "feat: migrate and mirror permanent member memories"
```

### Task 5: 文档、全量验证与生产迁移

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: 更新公开文档**

在 README 的成员记忆段替换为：

```markdown
成员记忆独立于随机回复概率持续收集。原始特性和历史昵称以追加式账本永久保存在服务器 SQLite 中，不再按 8 条上限淘汰；本地 JSON 镜像包含完整历史。每累计 5 条新特性会生成一次不超过 300 字的滚动摘要，聊天 AI 只读取摘要、最多 5 个近期旧称和最多 8 条尚未摘要的特性。`MEMBER_MEMORY_SUMMARY_ENABLED=false` 只关闭摘要生成，永久账本仍继续写入。真实成员记忆与镜像不会提交到 GitHub。
```

在 CHANGELOG 顶部增加：

```markdown
## [Unreleased]

### 群友记忆

- 原始特性和历史昵称改为追加式永久账本，不再因 8 条兼容视图上限淘汰。
- 每 5 条新特性生成滚动摘要，聊天只读取摘要与有界的新特性。
- 迁移当前仍保留的记忆，不回溯或重新提炼历史聊天。
```

- [ ] **Step 2: 运行全量验证**

Run:

```bash
/opt/qq-violation-bot/.venv/bin/python -m unittest discover -s tests -v
/opt/qq-violation-bot/.venv/bin/python -m compileall -q bot.py plugins scripts tests
/opt/qq-violation-bot/.venv/bin/python scripts/check_public_tree.py
git diff --check
```

Expected: 全部测试通过、编译退出 0、公开扫描 PASS、无空白错误。

- [ ] **Step 3: 提交文档**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: document permanent member memory summaries"
```

- [ ] **Step 4: 在线备份并运行生产 dry-run**

在服务器创建带时间戳的预迁移备份目录，复制 `data/chat_archive.db` 和 `data/member_memory/`，记录数据库 SHA-256。随后运行：

```bash
.venv/bin/python scripts/migrate_member_memory_v2.py --dry-run
```

Expected: 报告执行时的档案、特性和昵称数量，数据库 SHA-256 在 dry-run 前后完全一致。

- [ ] **Step 5: 短暂停服、最终备份并正式迁移**

```bash
systemctl stop qq-violation-bot.service
memory_backup_dir="/opt/qq-violation-bot/backups/member-memory-v2-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$memory_backup_dir"
install -m 0600 data/chat_archive.db "$memory_backup_dir/chat_archive.db"
cp -a data/member_memory "$memory_backup_dir/member_memory"
sha256sum "$memory_backup_dir/chat_archive.db" > "$memory_backup_dir/chat_archive.db.sha256"
.venv/bin/python scripts/migrate_member_memory_v2.py --apply
.venv/bin/python scripts/migrate_member_memory_v2.py --apply
```

Expected: 第一次迁移源数量与账本数量一致；第二次插入特性和昵称均为 0。通过只读统计确认诊断基线 65 条特性、4 条昵称均未缺失，若上线前数量增加则新数量也必须全部迁移。任一步失败时执行以下恢复，不继续启用摘要：

```bash
install -m 0600 "$memory_backup_dir/chat_archive.db" data/chat_archive.db
mv data/member_memory "${memory_backup_dir}/failed-member-memory"
cp -a "$memory_backup_dir/member_memory" data/member_memory
systemctl start qq-violation-bot.service
```

- [ ] **Step 6: 启用摘要、启动服务并生成初始摘要**

仅在服务器私有 `.env` 设置：

```dotenv
MEMBER_MEMORY_SUMMARY_ENABLED=true
```

启动 `qq-violation-bot.service`，确认 NoneBot 与 NapCat active、端口 6199 的反向 WebSocket 为 ESTAB、启动日志无导入/数据库错误。随后运行 `.venv/bin/python scripts/migrate_member_memory_v2.py --apply --summarize` 生成当前保留记忆的初始摘要；摘要失败只保留待处理状态，不回滚账本或服务。抽查统计与 JSON 字段，不输出真实昵称、特性正文或 QQ号。

- [ ] **Step 7: 最终回归与回滚检查**

再次运行全量测试和公开扫描，确认 Git 工作区只包含预期提交。验证将 `MEMBER_MEMORY_SUMMARY_ENABLED=false` 后读取会退化为最近 8 条事实但账本行数不变，再恢复生产配置为 `true`。
