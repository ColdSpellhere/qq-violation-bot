# Private Continuity, Relationship State, and Memory Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add restart-safe private-chat memory, independently versioned relationship state, and auditable superuser governance without changing any violation-business behavior.

**Architecture:** Store all new chat-memory state in the existing `chat_archive.db`, behind independent feature switches and the existing private allowlist. Persist raw private messages synchronously, process summaries and relationship updates through a recoverable leased job queue, and protect state updates with watermarks plus optimistic versions. Keep governance in a dedicated plugin whose parser and service are independent of the chat model.

**Tech Stack:** Python 3.10+, NoneBot2, OneBot V11, SQLite, `unittest`, existing feature-control JSON, existing online SQLite backup patterns.

---

## File map

Create:

- `plugins/private_memory/__init__.py` — plugin lifecycle registration only.
- `plugins/private_memory/models.py` — immutable records and enums shared by stores and services.
- `plugins/private_memory/schema.py` — idempotent schema migration and schema-version checks.
- `plugins/private_memory/store.py` — private messages, summaries, facts, retention, and export-safe reads.
- `plugins/private_memory/relationship.py` — scoped relationship reads and optimistic commits.
- `plugins/private_memory/ai.py` — domain prompts, strict parsing, and temporary direct model transport before Gateway migration.
- `plugins/private_memory/processor.py` — private summary, stable-fact, and relationship job handlers.
- `plugins/private_memory/jobs.py` — persistent job enqueue, claim, finish, lease recovery, and worker loop.
- `plugins/private_memory/lifecycle.py` — startup migration/recovery and bounded shutdown.
- `plugins/memory_governance/__init__.py` — governance matcher plugin entry.
- `plugins/memory_governance/commands.py` — strict `/记忆` parser and user-facing result formatting.
- `plugins/memory_governance/service.py` — preview, confirm, audit, soft delete, correction, relation update, and private-layer clear transactions.
- `plugins/memory_governance/matcher.py` — priority-0 superuser matcher and private receipt delivery.
- `scripts/migrate_private_memory.py` — explicit preflight, online backup, idempotent migration, and verification command.
- `tests/test_private_memory_schema.py`
- `tests/test_private_memory_store.py`
- `tests/test_relationship_state.py`
- `tests/test_memory_jobs.py`
- `tests/test_private_memory_processing.py`
- `tests/test_memory_governance_commands.py`
- `tests/test_memory_governance_service.py`
- `tests/test_memory_governance_matcher.py`
- `tests/test_private_memory_integration.py`

Modify:

- `plugins/violation_record/config.py` — safe defaults and retention/worker limits.
- `.env.example` — documented, disabled defaults only.
- `plugins/feature_control/state.py` — three first-stage memory switches with legacy-state migration.
- `plugins/feature_control/commands.py` — expose switch state and superuser controls without identifiers.
- `plugins/private_chat/conversation.py` — per-user lock registry plus store-backed snapshot adapter.
- `plugins/private_chat/matcher.py` — synchronous private-message persistence and post-send assistant persistence.
- `plugins/member_memory/store.py` — hide deleted or superseded group facts from normal reads and prompts.
- `plugins/member_memory/matcher.py` — optionally enqueue group relationship updates.
- `bot.py` — explicit loading of private-memory lifecycle and governance matcher.
- `tests/test_feature_control.py`
- `tests/test_feature_control_commands.py`
- `tests/test_private_chat.py`
- `tests/test_member_memory.py`
- `tests/test_plugin_loading.py`
- `README.md`
- `CHANGELOG.md`

Do not modify `plugins/violation_record/service.py`, business schemas, deduction policy, moderation, or business matcher behavior.

### Task 1: Add safe configuration and runtime gates

**Files:**

- Modify: `plugins/violation_record/config.py`
- Modify: `plugins/feature_control/state.py`
- Modify: `plugins/feature_control/commands.py`
- Modify: `.env.example`
- Test: `tests/test_feature_control.py`
- Test: `tests/test_feature_control_commands.py`

- [ ] **Step 1: Write failing tests for disabled defaults and legacy JSON loading**

Add assertions that a fresh configuration has all new switches disabled, and that a runtime JSON written by v1.0.4 loads with the missing keys supplied from safe defaults:

```python
state = FeatureController(path, defaults).snapshot()
assert state.private_memory_enabled is False
assert state.relationship_state_enabled is False
assert state.memory_governance_enabled is False

path.write_text(json.dumps({
    "business_enabled": True,
    "chat_enabled": True,
    "group_chat_enabled": True,
    "private_chat_enabled": True,
    "group_chat_allowed_group_ids": [100],
    "private_chat_allowed_user_ids": ["200"],
}), encoding="utf-8")
loaded = FeatureController(path, defaults).snapshot()
assert loaded.private_memory_enabled is False
```

Also test `/模块状态` shows only switch values and counts, never allowlist identifiers.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest \
  tests.test_feature_control tests.test_feature_control_commands -v
```

Expected: failure because `FeatureState` does not accept the new fields.

- [ ] **Step 3: Add config values and backward-compatible state fields**

Extend `AppConfig` with:

```python
private_memory_enabled: bool = _bool_env("PRIVATE_MEMORY_ENABLED", False)
relationship_state_enabled: bool = _bool_env("RELATIONSHIP_STATE_ENABLED", False)
memory_governance_enabled: bool = _bool_env("MEMORY_GOVERNANCE_ENABLED", False)
private_memory_retention_days: int = max(1, _int_env("PRIVATE_MEMORY_RETENTION_DAYS", 30))
private_memory_max_messages: int = max(1, _int_env("PRIVATE_MEMORY_MAX_MESSAGES", 500))
private_memory_shutdown_timeout: float = max(0.1, _float_env("PRIVATE_MEMORY_SHUTDOWN_TIMEOUT", 10.0))
```

Add the three first-stage switches to `FeatureState`, `SWITCH_NAMES`, `_load_state()`, status formatting, and the switch command map:

```python
"/私聊记忆": ("private_memory_enabled", "私聊持久记忆"),
"/关系状态": ("relationship_state_enabled", "关系状态"),
"/记忆治理": ("memory_governance_enabled", "记忆治理"),
```

When old persisted JSON lacks these keys, use the controller's configured defaults rather than rejecting the entire file. Keep strict boolean validation when a key is present.

- [ ] **Step 4: Document disabled defaults in `.env.example`**

Add exactly:

```dotenv
PRIVATE_MEMORY_ENABLED=false
RELATIONSHIP_STATE_ENABLED=false
MEMORY_GOVERNANCE_ENABLED=false
PRIVATE_MEMORY_RETENTION_DAYS=30
PRIVATE_MEMORY_MAX_MESSAGES=500
PRIVATE_MEMORY_SHUTDOWN_TIMEOUT=10
```

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the command from Step 2.

Expected: all feature-control tests pass.

- [ ] **Step 6: Commit**

```bash
git add .env.example plugins/violation_record/config.py plugins/feature_control/state.py \
  plugins/feature_control/commands.py tests/test_feature_control.py \
  tests/test_feature_control_commands.py
git commit -m "feat: add private memory runtime gates"
```

### Task 2: Add an idempotent chat-memory schema and backup preflight

**Files:**

- Create: `plugins/private_memory/models.py`
- Create: `plugins/private_memory/schema.py`
- Create: `scripts/migrate_private_memory.py`
- Test: `tests/test_private_memory_schema.py`

- [ ] **Step 1: Write schema tests against an empty and a legacy chat archive**

Test that `migrate(path)` creates the exact new tables, indexes, and `CHECK` constraints; running it twice must preserve rows and schema. Seed a legacy `chat_messages` table before migration and assert its row remains unchanged.

```python
first = migrate(database)
second = migrate(database)
assert first.schema_version == PRIVATE_MEMORY_SCHEMA_VERSION
assert second.schema_version == first.schema_version
assert connection.execute("SELECT plaintext FROM chat_messages").fetchone()[0] == "kept"
assert quick_check(database) == "ok"
```

Test that the migration command invokes an online backup before the first schema write and refuses to continue if backup verification or `quick_check` fails.

- [ ] **Step 2: Run the schema tests and verify RED**

```bash
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest \
  tests.test_private_memory_schema -v
```

Expected: import failure because `plugins.private_memory.schema` does not exist.

- [ ] **Step 3: Define immutable models and schema constants**

Define `ConversationScope`, `PrivateMessage`, `PrivateSummary`, `PrivateFact`, `RelationshipState`, `MemoryJob`, and `MigrationReport` as frozen dataclasses. Use string timestamps in UTC-compatible sortable format, string QQ IDs, and integer row/version watermarks.

The schema must create:

```sql
private_chat_messages
private_conversation_summaries
private_memory_facts
relationship_states
memory_jobs
memory_pending_operations
memory_governance_audit
private_memory_schema_meta
```

Add indexes for `(user_id, id)`, message expiry, active facts, relationship scope, runnable jobs, and confirmation-token expiry. Add `CHECK` constraints for direction, fact status/trust, conversation kind, job status/type, and audit result.

For the existing `member_memory_facts` table, add compatible columns idempotently: `trust_level` with legacy default `ai_extracted`, `status` with legacy default `active`, nullable `supersedes_id`, `updated_at`, `version` with default `1`, and nullable `deleted_at`. Backfill `updated_at` from `created_at`. Test both a genuine legacy table and a second migration after backfill.

- [ ] **Step 4: Implement migration and exact backup behavior**

Provide:

```python
def migrate(path: Path) -> MigrationReport: ...
def schema_version(path: Path) -> int: ...
def quick_check(path: Path) -> str: ...
def online_backup(source: Path, destination: Path) -> Path: ...
```

`scripts/migrate_private_memory.py` must accept `--database`, `--backup-dir`, and `--apply`. Without `--apply`, print only preflight results. With `--apply`, create a timestamped SQLite online backup, verify the backup, migrate, verify the result, and exit nonzero on any failure. Never delete an existing database or WAL file.

- [ ] **Step 5: Run schema tests and verify GREEN**

Run the command from Step 2.

Expected: all schema and migration tests pass.

- [ ] **Step 6: Commit**

```bash
git add plugins/private_memory/models.py plugins/private_memory/schema.py \
  scripts/migrate_private_memory.py tests/test_private_memory_schema.py
git commit -m "feat: add private memory schema migration"
```

### Task 3: Implement private message retention and isolated context reads

**Files:**

- Create: `plugins/private_memory/store.py`
- Test: `tests/test_private_memory_store.py`

- [ ] **Step 1: Write failing store tests**

Cover:

- user and assistant message idempotency;
- strict user isolation;
- assistant messages only when explicitly recorded after delivery;
- recent context order and limit;
- 500-message pruning;
- 30-day pruning;
- purged body removal while preserving hash/timestamp metadata;
- summaries and facts not removed by raw-message pruning;
- non-ASCII or non-positive user IDs rejected.

Use fixed timestamps and assert exact surviving IDs rather than only row counts.

- [ ] **Step 2: Run store tests and verify RED**

```bash
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest \
  tests.test_private_memory_store -v
```

Expected: import failure for `plugins.private_memory.store`.

- [ ] **Step 3: Implement the store API**

Provide these stable methods:

```python
class PrivateMemoryStore:
    def __init__(self, path: Path): ...
    def append_user_message(self, *, user_id: str, message_id: str,
                            text: str, event_time: int, source_kind: str) -> int: ...
    def append_assistant_message(self, *, user_id: str, source_message_id: str,
                                 bot_user_id: str, text: str, event_time: int) -> int: ...
    def recent_context(self, *, user_id: str, limit: int) -> tuple[ContextMessage, ...]: ...
    def get_summary(self, *, user_id: str) -> PrivateSummary | None: ...
    def commit_summary(self, *, user_id: str, summary_text: str,
                       source_start_id: int, source_end_id: int,
                       expected_through_id: int, expected_version: int) -> bool: ...
    def append_fact(self, candidate: PrivateFactCandidate,
                    *, trust_level: str = "ai_extracted") -> int | None: ...
    def active_facts(self, *, user_id: str, limit: int) -> tuple[PrivateFact, ...]: ...
    def purge_expired(self, *, now: datetime, retention_days: int,
                      max_messages: int) -> PurgeReport: ...
    def clear_private_layers(self, *, user_id: str, actor: str,
                             reason: str, operation_id: int) -> ClearReport: ...
```

All mutating methods use explicit transactions. Hash normalized UTF-8 text with SHA-256 before purging. Any retained `source_quote` is normalized and capped at 120 characters, and cannot contain the full original when that original exceeds the cap. `clear_private_layers()` removes raw bodies, summary, open topics, and pending private summary jobs, but not facts, relationship state, or audit rows.

- [ ] **Step 4: Run store tests and verify GREEN**

Run the command from Step 2.

Expected: all store tests pass.

- [ ] **Step 5: Commit**

```bash
git add plugins/private_memory/store.py tests/test_private_memory_store.py
git commit -m "feat: persist isolated private conversations"
```

### Task 4: Implement optimistic relationship state

**Files:**

- Create: `plugins/private_memory/relationship.py`
- Test: `tests/test_relationship_state.py`

- [ ] **Step 1: Write failing relationship tests**

Test group and private scopes, persona separation, length limits, five-topic limit, source watermark monotonicity, successful compare-and-swap, stale-version rejection, and failed transaction preservation.

```python
current = store.get_private(user_id="200", persona_id="radish-cat")
assert store.commit(candidate, expected_version=current.version) is True
assert store.commit(stale, expected_version=current.version) is False
assert store.get_private(user_id="200", persona_id="radish-cat").state_text == "new"
```

Add a boundary test proving no relationship module is imported by `plugins.violation_record` modules.

- [ ] **Step 2: Run relationship tests and verify RED**

```bash
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest \
  tests.test_relationship_state -v
```

Expected: import failure for `plugins.private_memory.relationship`.

- [ ] **Step 3: Implement scoped reads and compare-and-swap commits**

Provide:

```python
class RelationshipStore:
    def get_group(self, *, group_id: int, user_id: str,
                  persona_id: str) -> RelationshipState | None: ...
    def get_private(self, *, user_id: str,
                    persona_id: str) -> RelationshipState | None: ...
    def commit(self, candidate: RelationshipState,
               *, expected_version: int) -> bool: ...
```

Reject candidates over 600 characters, more than five topics, topics over 80 characters, non-monotonic watermarks, or invalid scopes. Use one conditional `UPDATE ... WHERE version=?` or a conflict-safe insert for version zero.

- [ ] **Step 4: Run relationship tests and verify GREEN**

Run the command from Step 2.

Expected: all relationship tests pass.

- [ ] **Step 5: Commit**

```bash
git add plugins/private_memory/relationship.py tests/test_relationship_state.py
git commit -m "feat: add versioned relationship state"
```

### Task 5: Add a recoverable memory-job queue and lifecycle

**Files:**

- Create: `plugins/private_memory/jobs.py`
- Create: `plugins/private_memory/lifecycle.py`
- Create: `plugins/private_memory/__init__.py`
- Modify: `bot.py`
- Test: `tests/test_memory_jobs.py`
- Test: `tests/test_plugin_loading.py`

- [ ] **Step 1: Write failing queue and lifecycle tests**

Cover enqueue idempotency, per-user ordering, different-user concurrency, lease expiry recovery, bounded retries, disabled-switch behavior, startup migration, startup recovery, and shutdown cancellation. Use a fake driver modeled after `tests/test_chat_vision_ingestion.py`.

Assert that shutdown waits for a fast task, then cancels a task exceeding the configured timeout while leaving its row recoverable.

- [ ] **Step 2: Run queue tests and verify RED**

```bash
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest \
  tests.test_memory_jobs tests.test_plugin_loading -v
```

Expected: import or plugin-loading failure for `plugins.private_memory`.

- [ ] **Step 3: Implement queue ownership and worker boundaries**

Provide:

```python
class MemoryJobQueue:
    def enqueue(self, *, job_type: str, conversation_kind: str,
                user_id: str, group_id: int | None,
                input_through_id: int, expected_version: int) -> int: ...
    def recover_expired_leases(self, *, now: datetime) -> int: ...
    def claim(self, *, worker_id: str, now: datetime,
              limit: int) -> tuple[MemoryJob, ...]: ...
    def finish(self, job: MemoryJob, *, worker_id: str,
               status: str, error_code: str = "") -> bool: ...
```

Enforce one active job per type/scope/watermark. Claim in a transaction, use finite leases, and condition finish on owner plus claimed version. Do not store message bodies or prompts in the queue.

- [ ] **Step 4: Register startup and shutdown hooks**

On startup: migrate, recover expired leases, start a named worker task, and run retention once. On shutdown: stop intake, await the worker up to `private_memory_shutdown_timeout`, cancel if needed, and preserve uncommitted jobs for recovery. Make lifecycle setup idempotent per driver.

Load `plugins.private_memory` explicitly in `bot.py`, and assert its exact module name in plugin-loading tests.

- [ ] **Step 5: Run queue tests and verify GREEN**

Run the command from Step 2.

Expected: all queue, lifecycle, and plugin-loading tests pass.

- [ ] **Step 6: Commit**

```bash
git add plugins/private_memory/__init__.py plugins/private_memory/jobs.py \
  plugins/private_memory/lifecycle.py bot.py tests/test_memory_jobs.py \
  tests/test_plugin_loading.py
git commit -m "feat: add recoverable private memory jobs"
```

### Task 6: Process summaries, stable facts, and relationship updates

**Files:**

- Create: `plugins/private_memory/ai.py`
- Create: `plugins/private_memory/processor.py`
- Modify: `plugins/private_memory/jobs.py`
- Modify: `plugins/member_memory/matcher.py`
- Test: `tests/test_private_memory_processing.py`
- Test: `tests/test_member_memory.py`

- [ ] **Step 1: Write failing processing tests**

Cover private rolling-summary generation, stable-fact extraction with source message and a maximum 120-character quote, private relationship updates, optional group relationship enqueueing, malformed model output, model failure, version conflict, and disabled-switch behavior. Assert that uncertain statements remain explicitly uncertain and that sensitive/private credentials are rejected by the existing conservative memory filter.

Use a fake model callable so tests never access the network:

```python
processor = PrivateMemoryProcessor(
    store=store,
    relationship_store=relationships,
    summarize=AsyncMock(return_value="新的滚动摘要"),
    extract=AsyncMock(return_value=(candidate,)),
    update_relationship=AsyncMock(return_value=relationship_candidate),
)
assert await processor.process(job) is True
```

- [ ] **Step 2: Run processing tests and verify RED**

```bash
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest \
  tests.test_private_memory_processing tests.test_member_memory -v
```

Expected: import failure because the processor does not exist.

- [ ] **Step 3: Implement domain prompts and strict output parsing**

Keep three separate prompt/contract functions in `plugins/private_memory/ai.py`:

```python
async def summarize_private_conversation(previous: str,
                                         messages: Sequence[PrivateMessage]) -> str | None: ...
async def extract_private_facts(messages: Sequence[PrivateMessage]) -> tuple[PrivateFactCandidate, ...]: ...
async def generate_relationship_candidate(current: RelationshipState | None,
                                           messages: Sequence[PrivateMessage]) -> RelationshipCandidate | None: ...
```

Before the unified Gateway exists, use the same configured DeepSeek endpoint and `httpx` pattern as existing memory calls. Do not share prompts between the three functions. Parse JSON strictly, cap every field before returning it, reject unknown certainty claims, and log only exception classes.

- [ ] **Step 4: Implement job handlers with watermarks**

`PrivateMemoryProcessor.process()` must reload committed messages by the job's user/scope and `input_through_id`, call the matching domain function, then commit only when the stored summary watermark or relationship version still matches `job.expected_version`. Fact insertion must be idempotent on scope, normalized text, and source message. A model or parse failure leaves the old state unchanged and returns a retryable/non-retryable result to the queue.

When `RELATIONSHIP_STATE_ENABLED` is true, `plugins/member_memory/matcher.py` may enqueue a group relationship job after archiving a new eligible group message. It must not call the model inline, change random reply probability, or import any violation-business service.

- [ ] **Step 5: Run processing tests and verify GREEN**

Run the command from Step 2.

Expected: all processing and existing member-memory tests pass.

- [ ] **Step 6: Commit**

```bash
git add plugins/private_memory/ai.py plugins/private_memory/processor.py \
  plugins/private_memory/jobs.py plugins/member_memory/matcher.py \
  tests/test_private_memory_processing.py tests/test_member_memory.py
git commit -m "feat: process layered private memory"
```

### Task 7: Integrate persistent private context without changing delivery semantics

**Files:**

- Modify: `plugins/private_chat/conversation.py`
- Modify: `plugins/private_chat/matcher.py`
- Test: `tests/test_private_chat.py`
- Test: `tests/test_private_memory_integration.py`

- [ ] **Step 1: Write failing integration tests**

Test:

- an allowed user message is committed before AI is called;
- AI failure preserves the user turn;
- send failure does not persist a fake assistant turn;
- successful send persists the assistant turn;
- a new `PrivateConversation` instance restores the same user's context;
- two users never share context or locks;
- disabling private memory preserves the old 20-turn in-memory behavior;
- removing a user from the allowlist prevents new persistence and job enqueueing;
- summaries, facts, relation and open topics are passed only to the private chat call boundary.

- [ ] **Step 2: Run integration tests and verify RED**

```bash
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest \
  tests.test_private_chat tests.test_private_memory_integration -v
```

Expected: persistent context assertions fail against the current deque-only implementation.

- [ ] **Step 3: Add a store-backed conversation adapter**

Keep `PrivateConversation` as the per-user synchronization boundary, but allow it to read from `PrivateMemoryStore` when enabled:

```python
class PrivateConversation:
    def __init__(self, limit: int = 20, *, user_id: str = "",
                 store: PrivateMemoryStore | None = None): ...
    def snapshot(self) -> tuple[ContextMessage, ...]: ...
    def append_user(self, turn: ContextMessage, *, event_time: int) -> None: ...
    def append_assistant(self, turn: ContextMessage, *, event_time: int) -> None: ...
```

Keep one `asyncio.Lock` per user. Do not hold a global lock during model calls.

- [ ] **Step 4: Update matcher transaction order**

Within the existing per-user lock:

1. verify allowlist and switches again;
2. snapshot committed prior context;
3. persist the current user message;
4. enqueue summary/relationship work only when their switches are enabled;
5. call chat generation;
6. send QQ reply;
7. persist assistant text only after successful send.

Do not persist sticker file paths or binary image content as private raw text.

- [ ] **Step 5: Run private integration tests and verify GREEN**

Run the command from Step 2.

Expected: all private chat and persistence tests pass.

- [ ] **Step 6: Commit**

```bash
git add plugins/private_chat/conversation.py plugins/private_chat/matcher.py \
  tests/test_private_chat.py tests/test_private_memory_integration.py
git commit -m "feat: restore private context across restarts"
```

### Task 8: Implement strict `/记忆` parsing and preview tokens

**Files:**

- Create: `plugins/memory_governance/commands.py`
- Create: `plugins/memory_governance/service.py`
- Modify: `plugins/member_memory/store.py`
- Test: `tests/test_memory_governance_commands.py`
- Test: `tests/test_memory_governance_service.py`
- Test: `tests/test_member_memory.py`

- [ ] **Step 1: Write parser and service tests first**

Test every approved command form, real OneBot @ segment extraction, ASCII-only private IDs, `G-`/`P-` IDs, malformed commands, content length, non-whitelisted private target rejection, token expiry, token actor binding, one-time consumption, mandatory reason, transactional audit, correction history, soft deletion, and exact clear-layer scope.

The parser must return typed commands instead of performing writes:

```python
command = parse_memory_command(text, message)
assert command.action == "add_fact"
assert command.scope.kind == "group"
assert command.scope.user_id == "200"
```

- [ ] **Step 2: Run governance tests and verify RED**

```bash
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest \
  tests.test_memory_governance_commands tests.test_memory_governance_service -v
```

Expected: import failure for `plugins.memory_governance`.

- [ ] **Step 3: Implement command models and strict parsing**

Recognize only `/记忆` as the first token. Implement view, relation view/update, add, modify, delete, clear, status, confirm, cancel, and help. Reject private IDs unless `value.isascii() and value.isdigit() and int(value) > 0`. Require real `at` message segments for group targets; do not trust display text such as `@张三`.

- [ ] **Step 4: Implement preview and transactional confirmation**

Provide:

```python
class MemoryGovernanceService:
    def preview(self, command: MemoryCommand, *, actor: str,
                now: datetime) -> PreviewResult: ...
    def confirm(self, token: str, *, actor: str,
                reason: str, now: datetime) -> CommitResult: ...
    def cancel(self, token: str, *, actor: str,
               now: datetime) -> CancelResult: ...
    def view(self, command: MemoryCommand, *, actor: str) -> ViewResult: ...
```

Generate a cryptographically random one-time token, store only its hash, expire it after ten minutes, and bind it to actor plus canonical operation payload. Apply the change and insert the audit record in one `BEGIN IMMEDIATE` transaction. Admin-added facts use trust `admin_confirmed`; modifications insert a new fact and mark the old row `superseded`; deletion sets `status='deleted'` and `deleted_at`.

Update normal group-member reads, summary batches, JSON mirrors, and prompt profiles to select only `status='active'`. Existing legacy rows remain active through migration defaults. A deleted or superseded group fact must disappear from future summaries and prompts but remain available to governance audit views.

- [ ] **Step 5: Run governance tests and verify GREEN**

Run the command from Step 2.

Expected: all command and transactional service tests pass.

- [ ] **Step 6: Commit**

```bash
git add plugins/memory_governance/commands.py plugins/memory_governance/service.py \
  plugins/member_memory/store.py tests/test_memory_governance_commands.py \
  tests/test_memory_governance_service.py tests/test_member_memory.py
git commit -m "feat: add auditable memory governance service"
```

### Task 9: Add the priority-0 governance matcher and private receipts

**Files:**

- Create: `plugins/memory_governance/__init__.py`
- Create: `plugins/memory_governance/matcher.py`
- Modify: `bot.py`
- Modify: `tests/test_plugin_loading.py`
- Test: `tests/test_memory_governance_matcher.py`

- [ ] **Step 1: Write matcher tests**

Assert:

- exact `/记忆` prefix is recognized before group/private chat matchers;
- non-superusers receive a refusal and cause no read or write;
- disabled governance returns a disabled message;
- content views are delivered only by private message to the superuser;
- private delivery failure never falls back to a group message;
- database commit failure reports no change;
- committed change plus receipt failure reports the distinct committed/receipt-failed state;
- malformed commands never reach the chat model.

- [ ] **Step 2: Run matcher tests and verify RED**

```bash
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest \
  tests.test_memory_governance_matcher tests.test_plugin_loading -v
```

Expected: matcher module is missing.

- [ ] **Step 3: Implement and load the matcher**

Register with `priority=0`, `block=True`, and a rule that only matches `/记忆` as the first token. Check `get_driver().config.superusers` before parsing or accessing storage. Use `bot.send_private_msg()` for content-bearing results and `matcher.finish()` only for non-sensitive acknowledgements.

Load `plugins.memory_governance` explicitly after feature control and before chat plugins. Extend plugin-loading tests to assert the exact module and matcher priority.

- [ ] **Step 4: Run matcher tests and verify GREEN**

Run the command from Step 2.

Expected: all matcher and plugin-loading tests pass.

- [ ] **Step 5: Commit**

```bash
git add plugins/memory_governance/__init__.py plugins/memory_governance/matcher.py \
  bot.py tests/test_memory_governance_matcher.py tests/test_plugin_loading.py
git commit -m "feat: expose superuser memory governance commands"
```

### Task 10: Add retention scheduling, operational documentation, and final verification

**Files:**

- Modify: `plugins/private_memory/lifecycle.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Test: `tests/test_memory_jobs.py`
- Test: `tests/test_public_source.py`
- Test: `tests/test_public_scanner.py`

- [ ] **Step 1: Add a failing daily-retention lifecycle test**

Use a fake clock and assert startup performs one retention pass, the daily loop schedules subsequent passes, disabled private memory does not persist new content, and shutdown cancels the loop cleanly.

- [ ] **Step 2: Run the focused lifecycle test and verify RED**

```bash
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest \
  tests.test_memory_jobs -v
```

Expected: the daily-retention assertion fails.

- [ ] **Step 3: Implement daily retention without VACUUM on the hot path**

Run `purge_expired()` at startup and every 24 hours. Apply `PRAGMA secure_delete=ON` for delete transactions and checkpoint WAL after a successful purge. Do not run `VACUUM` automatically; document it as an optional offline maintenance step.

- [ ] **Step 4: Document privacy, commands, migration, enablement, export, and rollback**

README must include:

- exact retained layers and 500/30-day limits;
- white-list boundary and per-user isolation;
- `/记忆` commands and confirmation behavior;
- backup and dry-run migration commands;
- switch enablement order;
- post-release smoke checks;
- stop-loss switch commands;
- code rollback versus database restore distinction;
- statement that historical messages are not reprocessed.

CHANGELOG must describe only delivered behavior and must not include real IDs or chat content.

- [ ] **Step 5: Run complete phase-one verification**

```bash
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest \
  tests.test_private_memory_schema \
  tests.test_private_memory_store \
  tests.test_relationship_state \
  tests.test_memory_jobs \
  tests.test_private_memory_processing \
  tests.test_private_memory_integration \
  tests.test_memory_governance_commands \
  tests.test_memory_governance_service \
  tests.test_memory_governance_matcher \
  tests.test_private_chat \
  tests.test_feature_control \
  tests.test_feature_control_commands \
  tests.test_plugin_loading -v
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest discover -s tests -v
TARGET_GROUP_ID=918273645 .venv/bin/python -m compileall -q bot.py plugins scripts tests
.venv/bin/python scripts/check_public_tree.py --history
git diff --check
git status --short
```

Expected: every test command exits zero, compilation exits zero, public scan reports PASS, diff check is silent, and status contains only intended source/document changes.

- [ ] **Step 6: Verify migration twice on a copied fixture**

Create a temporary copy of a test chat database, run the migration command twice with `--apply`, then assert `PRAGMA quick_check` is `ok`, schema version is unchanged, and seeded legacy rows are identical. Do not use the production database for this step.

- [ ] **Step 7: Commit**

```bash
git add plugins/private_memory/lifecycle.py README.md CHANGELOG.md \
  tests/test_memory_jobs.py tests/test_public_source.py tests/test_public_scanner.py
git commit -m "docs: document private memory operations"
```

## Production rollout checkpoint

Implementation completion does not authorize production deployment. Before rollout, record the release commit and create verified online backups of `chat_archive.db`, `violation_records.db`, and `evidence.db`. Run the private-memory migration with all new switches off, restart, verify existing business/group/private/image paths, then enable governance, private persistence, and relationship updates one at a time. If any new path fails, disable only its switch before considering code rollback.
