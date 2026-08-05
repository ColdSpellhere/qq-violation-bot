# v1.0.2.2beta Context-Aware Random Chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make random group replies use up to 20 recent text messages from the last 30 minutes and change the default/production trigger probability to 10%.

**Architecture:** Add one read-only query to the existing chat archive and pass its structured result into the isolated random-chat AI client. Keep failure handling local to the plugin, avoid schema changes, and preserve every existing business matcher.

**Tech Stack:** Python 3.10, SQLite, NoneBot2, OneBot v11, httpx, unittest, systemd, Git

---

### Task 1: Read recent conversation context

**Files:**
- Modify: `plugins/chat_archive/db.py`
- Create: `tests/test_random_chat_context.py`

- [ ] **Step 1: Write failing archive query tests**

Create temporary `chat_messages` rows and assert the wished-for API:

```python
from plugins.chat_archive.db import recent_text_context

rows = recent_text_context(
    path,
    group_id=123,
    since_epoch=1000,
    limit=20,
    exclude_message_id="current",
    bot_user_id="999",
)
```

Tests must prove: only the target group and time window are read; at most the newest 20 rows are returned in chronological order; current message, bot sender, blank text and `/` commands are removed; nickname fallback order is card, nickname, user ID; missing database/table returns `[]`.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/dotenv -f .env run -- .venv/bin/python -m unittest tests.test_random_chat_context -v`

Expected: import failure for `recent_text_context`.

- [ ] **Step 3: Implement the minimal query**

Add a frozen value object:

```python
@dataclass(frozen=True)
class ContextMessage:
    nickname: str
    text: str
```

Implement one parameterized SQLite query ordered newest-first with `LIMIT`, then reverse the filtered result. Decode `sender_json` defensively; apply the filtering and nickname fallback required by the tests. Catch only missing-file, missing-table, malformed-JSON and malformed-row conditions needed for the documented `[]`/row-skip degradation.

- [ ] **Step 4: Verify GREEN and commit**

Run the focused context test and existing `tests.test_chat_archive`; expect all pass.

```bash
git add plugins/chat_archive/db.py tests/test_random_chat_context.py
git commit -m "feat: read recent group chat context"
```

### Task 2: Send structured context to AI

**Files:**
- Modify: `plugins/random_chat/ai.py`
- Modify: `tests/test_random_chat.py`

- [ ] **Step 1: Write failing AI payload tests**

Change the wished-for call to:

```python
reply = await generate_reply(
    "今晚吃什么",
    context=[ContextMessage("小明", "想吃火锅"), ContextMessage("小红", "我也想")],
)
```

Assert the request contains the two context lines in chronological order, labels the current sender message separately, requests natural topic-following group chat, and still supports `context=[]`.

- [ ] **Step 2: Verify RED**

Run the focused random-chat tests; expect `TypeError` because `context` is not accepted.

- [ ] **Step 3: Implement minimal prompt construction**

Add a required keyword-only `context: Sequence[ContextMessage] = ()`. Build one user message containing `近期群聊` lines and `当前消息`; do not include raw JSON, IDs, images or database fields. Update the system prompt to stay on topic, avoid forced answers/repetition, cap output at two sentences, and forbid invented identity/actions.

- [ ] **Step 4: Verify GREEN and commit**

Run `tests.test_random_chat`; expect all pass.

```bash
git add plugins/random_chat/ai.py tests/test_random_chat.py
git commit -m "feat: generate context-aware group replies"
```

### Task 3: Integrate context with safe degradation

**Files:**
- Modify: `plugins/random_chat/matcher.py`
- Modify: `plugins/violation_record/config.py`
- Modify: `.env.example`
- Modify: `tests/test_random_chat_context.py`
- Modify: `tests/test_random_chat.py`

- [ ] **Step 1: Write failing integration tests**

Test a small async orchestration helper that reads context with `since_epoch=event.time-1800`, `limit=20`, current message ID and bot ID, then calls `generate_reply(text, context=rows)`. Assert archive errors produce `context=[]`; AI errors and `send_group_msg` errors are caught and logged rather than escaping.

- [ ] **Step 2: Verify RED**

Run both random-chat test modules; expect missing orchestration API or wrong AI call signature.

- [ ] **Step 3: Implement integration and 10% default**

Keep matcher `priority=9, block=False`. Query `CONFIG.chat_archive_path` only after probability selection. Pass `int(event.time) - 1800`, `limit=20`, `str(event.message_id)` and `str(event.self_id)`. Catch archive exceptions independently and continue with an empty list. Catch both `RandomChatAIError` and OneBot send exceptions at the plugin boundary.

Change config and `.env.example` default from `0.05` to `0.10`, and update the default test accordingly.

- [ ] **Step 4: Verify GREEN and commit**

Run focused context, random-chat, plugin-loading and chat-archive tests; expect all pass. Run compileall and `git diff --check`.

```bash
git add plugins/random_chat/matcher.py plugins/violation_record/config.py .env.example tests/test_random_chat.py tests/test_random_chat_context.py
git commit -m "feat: integrate contextual random chat"
```

### Task 4: Release docs and complete verification

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Document v1.0.2.2beta**

Document 20 messages/30 minutes, text-only filtering, nickname formatting, failure degradation, 10% probability, no schema migration and rollback to `v1.0.2.1beta`.

- [ ] **Step 2: Run full fresh verification**

```bash
.venv/bin/python -m compileall -q bot.py plugins/random_chat plugins/chat_archive plugins/violation_record/config.py
.venv/bin/dotenv -f .env run -- .venv/bin/python -m unittest tests.test_random_chat tests.test_random_chat_context tests.test_chat_archive tests.test_plugin_loading -v
.venv/bin/dotenv -f .env run -- .venv/bin/python -m unittest discover -s tests -v
git diff --check
git status --short
```

Expected: focused and full suites pass with zero failures; status contains only intended README/CHANGELOG changes before commit.

- [ ] **Step 3: Commit docs**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: release v1.0.2.2beta contextual chat"
```

### Task 5: Controlled deployment and tag

**Files:**
- Modify: `.env` (deployment-only, not committed)

- [ ] **Step 1: Record deployment and rollback commits**

Record `git rev-parse HEAD`, clean status, and retain `v1.0.2.1beta`/`c413bba` as rollback.

- [ ] **Step 2: Set production probability idempotently**

Run `.venv/bin/dotenv -f .env set RANDOM_CHAT_PROBABILITY 0.10`; leave every other key untouched.

- [ ] **Step 3: Restart and verify**

Restart only `qq-violation-bot.service`. Confirm both services active, port 6199 is established, logs have no import/config exceptions, and `load_dotenv + CONFIG` reports enabled with probability `0.1`.

- [ ] **Step 4: Create verified tag**

Run `git tag -a v1.0.2.2beta -m "v1.0.2.2beta"`. Verify the peeled tag points to the deployment commit. Do not push or merge without explicit user authorization.

- [ ] **Step 5: Preserve rollback**

Fast rollback: set probability to `0.05` or disable the feature, then restart NoneBot. Code rollback: switch to `v1.0.2.1beta` and restart; no database rollback is needed.
