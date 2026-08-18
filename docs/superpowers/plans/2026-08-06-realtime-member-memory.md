# Realtime Member Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist conservative group-member memories in near real time without coupling memory extraction to the bot's 1% random-reply probability.

**Architecture:** Add a small per-member micro-batcher and a dedicated non-blocking NoneBot matcher. The matcher reads archived context and reuses the existing AI extraction and validated storage functions; the random-chat matcher only generates replies.

**Tech Stack:** Python 3.10, asyncio, NoneBot2, OneBot v11, SQLite, unittest

---

### Task 1: Per-member micro-batcher

**Files:**
- Create: `plugins/member_memory/batcher.py`
- Test: `tests/test_member_memory_batcher.py`

- [ ] **Step 1: Write failing tests for count, timer, isolation, and serialization**

Create tests using `unittest.IsolatedAsyncioTestCase`, `AsyncMock`, and a short `delay_seconds=0.02`. Verify that the fifth call for one `(group_id, user_id)` invokes the callback once immediately, one message invokes it after the delay, two different users flush independently, and two batches for one user never execute their callbacks concurrently.

The public API exercised by the tests is:

```python
batcher = MemberMemoryBatcher(threshold=5, delay_seconds=0.02)
batcher.add(group_id=123, user_id="456", event_time=1000, callback=callback)
await batcher.drain()
```

- [ ] **Step 2: Run the tests and verify the expected failure**

Run: `.venv/bin/python -m unittest tests.test_member_memory_batcher -v`

Expected: FAIL because `plugins.member_memory.batcher` does not exist.

- [ ] **Step 3: Implement the minimal batcher**

Implement `MemberMemoryBatcher` with:

```python
@dataclass
class _PendingBatch:
    count: int
    latest_event_time: int
    timer: asyncio.Task[None]

class MemberMemoryBatcher:
    def __init__(self, *, threshold: int = 5, delay_seconds: float = 60.0): ...
    def add(self, *, group_id: int, user_id: str, event_time: int,
            callback: Callable[[int, str, int], Awaitable[None]]) -> None: ...
    async def drain(self) -> None: ...
```

Use `(group_id, user_id)` as the batch key. On the fifth message, cancel the timer and schedule a flush immediately. Timer and threshold paths must atomically remove the same pending batch so only one wins. Wrap callbacks in a per-key `asyncio.Lock` so two completed batches for one member run serially. Track callback tasks so `drain()` can await them in tests.

- [ ] **Step 4: Run the focused tests**

Run: `.venv/bin/python -m unittest tests.test_member_memory_batcher -v`

Expected: all batcher tests PASS.

### Task 2: Dedicated memory matcher

**Files:**
- Create: `plugins/member_memory/matcher.py`
- Modify: `plugins/member_memory/__init__.py`
- Modify: `bot.py`
- Modify: `tests/test_member_memory.py`
- Modify: `tests/test_plugin_loading.py`

- [ ] **Step 1: Write failing matcher tests**

Add tests proving:

```python
# A target-group normal text message is submitted even when random chat would not reply.
with patch("plugins.member_memory.matcher.BATCHER.add") as add:
    await collect_member_memory(event)
add.assert_called_once()

# Commands, blank messages, self messages, and messages outside the target group are ignored.

# Analysis reads recent archived context, calls extract_memory_candidates,
# keeps candidates whose user_id matches the batched member, and calls apply_candidates.
```

Patch external AI calls; do not make network requests in tests.

- [ ] **Step 2: Run the focused test and verify the expected failure**

Run: `.venv/bin/python -m unittest tests.test_member_memory tests.test_plugin_loading -v`

Expected: FAIL because `plugins.member_memory.matcher` does not exist.

- [ ] **Step 3: Implement the matcher and analysis callback**

Create a matcher with `priority=2` and `block=False`. Its rule accepts only `GroupMessageEvent` events for `CONFIG.target_group_id` and rejects `event.user_id == event.self_id`. The handler strips plaintext and ignores blank or `/`-prefixed text before calling the singleton batcher.

Import the matcher from `plugins/member_memory/__init__.py`, add `nonebot.load_plugin("plugins.member_memory")` to `bot.py` after chat archive loading, and extend the plugin-loading regression test to require the new plugin.

Implement the callback as:

```python
async def analyze_member_memory(group_id: int, user_id: str, event_time: int) -> None:
    context = recent_text_context(
        CONFIG.chat_archive_path,
        group_id=group_id,
        since_epoch=event_time - 1800,
        limit=20,
        exclude_message_id="",
        bot_user_id=str(CONFIG.bot_self_id),
    )
    candidates = await extract_memory_candidates(context)
    member_candidates = [
        item for item in candidates if str(item.get("user_id") or "") == user_id
    ]
    apply_candidates(
        CONFIG.chat_archive_path,
        CONFIG.member_memory_root,
        group_id=group_id,
        context=context,
        candidates=member_candidates,
    )
```

Catch and log failures at the callback boundary so they cannot affect other matchers.

- [ ] **Step 4: Run the focused tests**

Run: `.venv/bin/python -m unittest tests.test_member_memory tests.test_member_memory_batcher tests.test_plugin_loading -v`

Expected: all focused tests PASS.

### Task 3: Remove memory extraction from random replies

**Files:**
- Modify: `plugins/random_chat/matcher.py`
- Modify: `tests/test_random_chat_context.py`

- [ ] **Step 1: Change the regression expectation first**

Update `RandomChatIntegrationTests` so `send_random_reply()` is expected to call only `generate_reply()` and `bot.send_group_msg()`. Remove expectations that it calls `extract_memory_candidates()` or `apply_candidates()`.

Add a separate assertion that the random matcher still calls `should_reply(CONFIG.random_chat_probability)` and returns without sending when that function is false; this proves the reply probability remains independent.

- [ ] **Step 2: Run the test and verify the expected failure**

Run: `.venv/bin/python -m unittest tests.test_random_chat_context -v`

Expected: FAIL because `send_random_reply()` still invokes memory extraction.

- [ ] **Step 3: Remove the coupled code**

Delete the `extract_memory_candidates` and `apply_candidates` imports and the final memory-update `try` block from `plugins/random_chat/matcher.py`. Do not change `should_reply`, its probability argument, reply generation, or sending behavior.

- [ ] **Step 4: Run focused random-chat tests**

Run: `.venv/bin/python -m unittest tests.test_random_chat tests.test_random_chat_context -v`

Expected: all random-chat tests PASS.

### Task 4: Documentation and full verification

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/releases/2026-08-06-v1.0.2.3beta.md`

- [ ] **Step 1: Document the behavior**

State that memory collection uses a five-message-or-60-second per-member batch, runs independently of random replies, keeps the existing conservative evidence checks, and leaves `RANDOM_CHAT_PROBABILITY=0.01` unchanged in deployment configuration.

- [ ] **Step 2: Run the complete test suite**

Run: `.venv/bin/python -m unittest discover -s tests -v`

Expected: all tests PASS with zero failures and errors.

- [ ] **Step 3: Inspect the complete diff and runtime configuration**

Run: `git diff --check && git diff --stat && grep '^RANDOM_CHAT_PROBABILITY=' .env`

Expected: no whitespace errors; only scoped source, tests, and documentation changes; probability is `0.01`.

- [ ] **Step 4: Deploy with rollback backup**

Back up every changed server file before replacement, copy the tested files into `/opt/qq-violation-bot`, restart only `qq-violation-bot.service`, and leave `napcat.service` running.

- [ ] **Step 5: Verify production health**

Run service checks, verify the reverse OneBot socket on `127.0.0.1:6199` is `ESTAB`, confirm `BOT_SELF_ID=2727968581` and `RANDOM_CHAT_PROBABILITY='0.01'`, and inspect startup logs for import errors or memory matcher exceptions.

- [ ] **Step 6: Git handoff**

Do not commit, push, tag, or publish until the user explicitly authorizes version-control writes.
