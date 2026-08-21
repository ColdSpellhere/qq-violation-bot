# Modular Feature Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add persistent QQ-administered business/chat switches, independent group/private allowlists, isolated group routing, and one-shot summaries for business reminders missed while disabled or offline.

**Architecture:** Keep NoneBot plugins registered and put a shared `FeatureController` in front of every matcher and scheduled send. A thin group router sends recognized business requests to the business handler and all other addressed/eligible messages to chat; archive and member-memory matchers consult the same chat gate. Existing policy outbox rows are retained and summarized through one merged-forward message after delivery recovers.

**Tech Stack:** Python 3.11+, NoneBot 2, OneBot v11, SQLite, JSON runtime state, `unittest`

**Spec:** `docs/plans/2026-08-21-modular-feature-control-design.md`

## Global Constraints

- Business capability is available only in `CONFIG.target_group_id`; other groups never enter business intent parsing.
- `chat_enabled` is the parent of group chat, private chat, chat archive, and member memory.
- Group chat and private chat have independent child switches and independent allowlists.
- In an allowed chat group, an explicit bot mention always gets a chat response when no business request handles it; ordinary text remains probability-gated.
- Runtime state is stored in `data/runtime_features.json`, is atomically replaced with a backup, and is not committed to Git.
- Only NoneBot `SUPERUSERS` can inspect or change runtime switches and allowlists.
- Missed business reminders are never replayed individually; recovery emits one merged-forward overview and marks rows handled only after success.
- Existing business data, persona, sticker weighting, random probability, and member-memory facts are preserved.
- Tests remain focused on gates, routing, persistence, and missed-message delivery; do not introduce new dependencies.

---

### Task 1: Persistent Feature Controller

**Files:**
- Create: `plugins/feature_control/__init__.py`
- Create: `plugins/feature_control/state.py`
- Create: `plugins/feature_control/runtime.py`
- Modify: `plugins/violation_record/config.py`
- Modify: `.gitignore`
- Test: `tests/test_feature_control.py`

**Interfaces:**
- Produces: immutable `FeatureState` with `business_enabled`, `chat_enabled`, `group_chat_enabled`, `private_chat_enabled`, `group_chat_allowed_group_ids`, `private_chat_allowed_user_ids`, `updated_at`, and `updated_by`.
- Produces: `FeatureController.snapshot()`, `set_switch(name, enabled, actor)`, `add_allowed(kind, value, actor)`, `remove_allowed(kind, value, actor)`, `business_allowed(group_id, target_group_id)`, `group_chat_allowed(group_id)`, and `private_chat_allowed(user_id)`.
- Produces: process singleton `plugins.feature_control.runtime.FEATURES`.

- [ ] **Step 1: Write persistence and gate tests**

Create `tests/test_feature_control.py` with focused cases equivalent to:

```python
def test_parent_and_child_gates_are_both_required(self):
    controller = FeatureController(path, defaults)
    self.assertTrue(controller.group_chat_allowed(100))
    controller.set_switch("chat_enabled", False, actor="1")
    self.assertFalse(controller.group_chat_allowed(100))
    self.assertFalse(controller.private_chat_allowed("200"))
    self.assertTrue(controller.business_allowed(999, 999))

def test_state_survives_restart_and_keeps_backup(self):
    first = FeatureController(path, defaults)
    first.add_allowed("group_chat", "101", actor="1")
    second = FeatureController(path, defaults)
    self.assertIn(101, second.snapshot().group_chat_allowed_group_ids)
    self.assertTrue(path.with_suffix(path.suffix + ".bak").is_file())

def test_invalid_write_keeps_old_in_memory_state(self):
    controller = FeatureController(path, defaults)
    with patch.object(controller, "_persist", side_effect=OSError("disk full")):
        with self.assertRaises(OSError):
            controller.set_switch("chat_enabled", False, actor="1")
    self.assertTrue(controller.snapshot().chat_enabled)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m unittest tests.test_feature_control -v`

Expected: FAIL because `plugins.feature_control.state` does not exist.

- [ ] **Step 3: Implement the state model and atomic controller**

Implement `plugins/feature_control/state.py` with strict switch names and allowlist kinds:

```python
SWITCH_NAMES = {
    "business_enabled", "chat_enabled",
    "group_chat_enabled", "private_chat_enabled",
}
ALLOWLIST_KINDS = {"group_chat", "private_chat"}

@dataclass(frozen=True)
class FeatureState:
    business_enabled: bool
    chat_enabled: bool
    group_chat_enabled: bool
    private_chat_enabled: bool
    group_chat_allowed_group_ids: tuple[int, ...]
    private_chat_allowed_user_ids: tuple[str, ...]
    updated_at: str = ""
    updated_by: str = ""
```

`FeatureController` must validate positive numeric IDs, copy the current JSON to `.json.bak` before each replacement, write a same-directory temporary file, call `os.replace`, and swap its in-memory state only after persistence succeeds. On startup it loads the primary file, then backup, then defaults.

- [ ] **Step 4: Add configuration defaults and runtime singleton**

Add comma-separated numeric-list parsing in `config.py`. Preserve legacy values by using:

```python
business_enabled = _bool_env("BUSINESS_ENABLED", True)
chat_enabled = _bool_env("CHAT_ENABLED", random_chat_enabled or private_chat_enabled)
group_chat_enabled = _bool_env("GROUP_CHAT_ENABLED", random_chat_enabled)
group_chat_allowed_group_ids = _id_tuple_env(
    "GROUP_CHAT_ALLOWED_GROUP_IDS", (_TARGET_GROUP_ID,)
)
private_chat_enabled = _bool_env("PRIVATE_CHAT_ENABLED", False)
private_chat_allowed_user_ids = _string_id_tuple_env(
    "PRIVATE_CHAT_ALLOWED_USER_IDS", legacy_private_ids
)
runtime_features_path = DATA_DIR / "runtime_features.json"
```

`runtime.py` constructs `FEATURES` from these values. Add `/data/runtime_features.json` and `/data/runtime_features.json.bak` to `.gitignore`.

- [ ] **Step 5: Run focused and configuration regression tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_feature_control tests.test_random_chat tests.test_private_chat -v
```

Expected: PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add .gitignore plugins/feature_control plugins/violation_record/config.py tests/test_feature_control.py
git commit -m "feat: add persistent feature controls"
```

### Task 2: QQ Superuser Control Commands

**Files:**
- Create: `plugins/feature_control/commands.py`
- Create: `plugins/feature_control/matcher.py`
- Modify: `bot.py`
- Test: `tests/test_feature_control_commands.py`
- Modify: `tests/test_plugin_loading.py`

**Interfaces:**
- Consumes: `FeatureController` from Task 1.
- Produces: `is_control_command(text) -> bool` and `execute_control_command(text, controller, actor) -> str`.
- Produces: NoneBot matcher that refuses non-superusers and calls the pure command executor for authorized users.

- [ ] **Step 1: Write command parser and authorization tests**

Cover all exact command forms and one unauthorized event:

```python
self.assertEqual("业务功能已关闭。", execute_control_command("/业务 关", controller, "1"))
self.assertEqual("已添加群聊群：123。", execute_control_command("/群聊群 添加 123", controller, "1"))
self.assertEqual("已添加私聊用户：456。", execute_control_command("/私聊用户 添加 456", controller, "1"))
self.assertIn("聊天总开关：开", execute_control_command("/模块状态", controller, "1"))
```

The matcher test patches `get_driver().config.superusers` and asserts a non-superuser gets `你没有模块管理权限。` without mutating state.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m unittest tests.test_feature_control_commands -v`

Expected: FAIL because command modules do not exist.

- [ ] **Step 3: Implement the pure command executor**

Support only these commands:

```text
/模块状态
/业务 开|关
/聊天 开|关
/群聊 开|关
/群聊群 添加 <群号>
/群聊群 删除 <群号>
/群聊群 列表
/私聊 开|关
/私聊用户 添加 <QQ号>
/私聊用户 删除 <QQ号>
/私聊用户 列表
```

Return concise Chinese success, duplicate/not-found, validation, and usage messages. `/模块状态` must show the four switches and counts, but detailed allowlists appear only in the explicit list commands.

- [ ] **Step 4: Register the superuser-only matcher**

Use `on_message(priority=0, block=True)` with a rule that matches only recognized control-command prefixes. In the handler, compare `str(event.user_id)` with `get_driver().config.superusers`; reject unauthorized callers before invoking the controller. Load `plugins.feature_control.matcher` explicitly from `bot.py` and assert its exact module name in `test_plugin_loading.py`.

- [ ] **Step 5: Run focused tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_feature_control_commands tests.test_plugin_loading -v
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add bot.py plugins/feature_control tests/test_feature_control_commands.py tests/test_plugin_loading.py
git commit -m "feat: manage feature switches from QQ"
```

### Task 3: Independent Group Router and Chat Gates

**Files:**
- Create: `plugins/group_router/__init__.py`
- Create: `plugins/group_router/matcher.py`
- Modify: `plugins/violation_record/matcher.py`
- Modify: `plugins/random_chat/matcher.py`
- Modify: `plugins/chat_archive/matcher.py`
- Modify: `plugins/member_memory/matcher.py`
- Modify: `plugins/private_chat/matcher.py`
- Modify: `bot.py`
- Test: `tests/test_group_router.py`
- Modify: `tests/test_violation_chat_fallback.py`
- Modify: `tests/test_random_chat_context.py`
- Modify: `tests/test_chat_archive.py`
- Modify: `tests/test_member_memory.py`
- Modify: `tests/test_private_chat.py`
- Modify: `tests/test_plugin_loading.py`

**Interfaces:**
- Consumes: `FEATURES` and `FeatureController` gates from Task 1.
- Produces: `handle_business_message(bot, event, text) -> bool`; `True` means a recognized business request sent a reply, `False` means the router may fall through to chat.
- Produces: `route_group_message(bot, event) -> None` as the only business/chat group response matcher.
- Retains: `send_random_reply(bot, event, text, addressed=False) -> bool` as a chat delivery service without registering its own group matcher.

- [ ] **Step 1: Write router and gate regression tests**

Add cases proving:

```python
async def test_non_business_group_never_calls_business_parser(self): ...
async def test_known_business_request_does_not_call_chat(self): ...
async def test_unknown_addressed_business_group_message_falls_through_to_chat(self): ...
async def test_addressed_allowed_group_always_chats_without_probability_sample(self): ...
async def test_ordinary_allowed_group_is_probability_gated(self): ...
async def test_chat_disabled_blocks_reply_archive_and_memory(self): ...
async def test_private_gate_requires_parent_child_and_allowlist(self): ...
```

Use real OneBot event objects and patch only external AI/send/storage boundaries.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_group_router tests.test_chat_archive tests.test_member_memory tests.test_private_chat -v
```

Expected: FAIL because the router does not exist and existing matchers only accept `TARGET_GROUP_ID`.

- [ ] **Step 3: Convert the business matcher into a callable handler**

Remove its `on_message` registration. Refactor the current handler into:

```python
async def handle_business_message(bot: Bot, event: GroupMessageEvent, text: str) -> bool:
    # fixed policy command -> send and True
    # parsed intent == "unknown" -> False without importing chat
    # known intent/error -> send and True
```

Keep evidence capture, structured replies, file upload, admin sync, mute handling, and existing error messages. The business module must not import `plugins.random_chat`.

- [ ] **Step 4: Add the thin group router**

Register one `on_message(priority=8, block=True)` matcher. Extract mention-free text once, then:

```python
if group_id == CONFIG.target_group_id and FEATURES.business_allowed(group_id, CONFIG.target_group_id) and addressed:
    if await handle_business_message(bot, event, text):
        return
if not FEATURES.group_chat_allowed(group_id):
    return
if addressed:
    await send_random_reply(bot, event, text, addressed=True)
elif eligible_text(text, at_bot=False) and should_reply(CONFIG.random_chat_probability):
    await send_random_reply(bot, event, text)
```

The candidate rule must accept only the current business group or an allowed chat group, and reject messages sent by the bot itself. Load this router explicitly from `bot.py`.

- [ ] **Step 5: Bind archive, memory, and private chat to the shared gates**

- Archive candidate: `FEATURES.group_chat_allowed(event.group_id)`; pass the actual event group to `archive_payload`.
- Memory candidate and delayed callback: check `FEATURES.group_chat_allowed(group_id)` both when enqueuing and before analysis writes.
- Private candidate: `FEATURES.private_chat_allowed(event.user_id)` and sender is not self.
- Remove the standalone random-chat matcher registration while retaining delivery helpers.

- [ ] **Step 6: Run all affected tests**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_group_router tests.test_violation_chat_fallback \
  tests.test_random_chat tests.test_random_chat_context \
  tests.test_chat_archive tests.test_member_memory tests.test_private_chat \
  tests.test_plugin_loading -v
```

Expected: PASS and no duplicate reply matcher remains.

- [ ] **Step 7: Commit Task 3**

```bash
git add bot.py plugins/group_router plugins/violation_record/matcher.py \
  plugins/random_chat/matcher.py plugins/chat_archive/matcher.py \
  plugins/member_memory/matcher.py plugins/private_chat/matcher.py tests
git commit -m "feat: route business and chat independently"
```

### Task 4: Missed Business Reminder Summary

**Files:**
- Modify: `plugins/violation_record/scheduler.py`
- Modify: `tests/test_policy_scheduler.py`

**Interfaces:**
- Consumes: `FEATURES.business_allowed(CONFIG.target_group_id, CONFIG.target_group_id)`.
- Produces: `defer_policy_outbox(reason, as_of) -> int` for pending rows that could not be sent.
- Produces: `deliver_missed_policy_summary(bot, as_of=None) -> int`, returning the number of rows marked handled.
- Changes: `deliver_policy_outbox` sends fresh `pending` rows individually but never retries `failed` rows individually.

- [ ] **Step 1: Add failing scheduler tests**

Add focused cases:

```python
def test_business_off_records_pending_rows_without_sending(self): ...
def test_no_bot_marks_pending_rows_as_offline(self): ...
def test_failed_rows_recover_as_one_forward_summary(self): ...
def test_summary_failure_keeps_rows_failed(self): ...
def test_successful_summary_marks_rows_sent_and_deduplicates(self): ...
```

The fake bot must record `call_api("send_group_forward_msg", ...)` separately from `send_group_msg`.

- [ ] **Step 2: Run scheduler tests and verify RED**

Run: `.venv/bin/python -m unittest tests.test_policy_scheduler -v`

Expected: FAIL because deferral and summary functions do not exist.

- [ ] **Step 3: Separate fresh delivery from missed delivery**

Change `_claim_policy_outbox` to claim only `status='pending'`. Add `defer_policy_outbox` that updates currently due pending rows to `status='failed'`, preserves their message and stable outbox ID, and stores one of `business_disabled` or `bot_offline` in `last_error`.

`maintenance_tick` must still run policy maintenance so missed reminders are generated, then:

```python
if not FEATURES.business_allowed(CONFIG.target_group_id, CONFIG.target_group_id):
    defer_policy_outbox("business_disabled", moment)
elif not bots:
    defer_policy_outbox("bot_offline", moment)
else:
    await deliver_missed_policy_summary(bot, as_of=moment)
    await deliver_policy_outbox(bot, as_of=moment, limit=_OUTBOX_DELIVERY_BATCH)
```

- [ ] **Step 4: Implement one merged-forward recovery summary**

Select valid failed rows in deterministic order, cancel invalid rows through the existing validation path, and build nodes containing:

```text
未发送业务提醒概览
时间范围：<first> 至 <last>
涉及提醒：<count> 条
原因：业务关闭 <n> / QQ离线 <n> / 发送失败 <n>
```

Follow the overview node with one concise node per deduplicated outbox row. Call `send_group_forward_msg` once. Only after success update those rows to `sent`, set `sent_at`, and clear `last_error`. On any exception, leave them `failed`.

- [ ] **Step 5: Run scheduler and policy regression tests**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_policy_scheduler tests.test_policy_integration \
  tests.test_deduction_policy -v
```

Expected: PASS.

- [ ] **Step 6: Commit Task 4**

```bash
git add plugins/violation_record/scheduler.py tests/test_policy_scheduler.py
git commit -m "feat: summarize missed business reminders"
```

### Task 5: Configuration, Operations Documentation, and Final Verification

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/plans/2026-08-21-modular-feature-control-design.md` only if implementation names differ from the approved design
- Test: existing suite

**Interfaces:**
- Consumes: exact environment names and QQ commands introduced in Tasks 1-4.
- Produces: operator-facing defaults, upgrade notes, rollback/disable commands, and OA future-extension note.

- [ ] **Step 1: Document safe defaults and compatibility**

Add these synthetic example keys without live QQ values:

```dotenv
BUSINESS_ENABLED=true
CHAT_ENABLED=false
GROUP_CHAT_ENABLED=false
GROUP_CHAT_ALLOWED_GROUP_IDS=123456789
PRIVATE_CHAT_ENABLED=false
PRIVATE_CHAT_ALLOWED_USER_IDS=
```

Keep legacy keys documented as compatibility inputs where applicable. Explain that `data/runtime_features.json` overrides defaults after first runtime mutation and is intentionally ignored by Git.

- [ ] **Step 2: Document QQ operations and recovery behavior**

README must list the exact management commands, parent/child behavior, business-group isolation, and missed-reminder merged overview. Record the future OA management platform as out of scope but supported by the single control-service boundary.

- [ ] **Step 3: Run static checks and focused full regression**

Run:

```bash
git diff --check
.venv/bin/python -m compileall -q bot.py plugins
.venv/bin/python -m unittest \
  tests.test_feature_control tests.test_feature_control_commands \
  tests.test_group_router tests.test_policy_scheduler \
  tests.test_chat_archive tests.test_member_memory tests.test_private_chat \
  tests.test_random_chat tests.test_random_chat_context \
  tests.test_violation_chat_fallback tests.test_plugin_loading -v
```

Expected: all checks pass.

- [ ] **Step 4: Run the broader suite in isolated test modules**

Run each `tests/test_*.py` module through unittest with `TARGET_GROUP_ID=999000111`, excluding only a test if its own documented public-secret scanner intentionally compares against that runtime value; run that scanner separately with a synthetic value absent from the tree.

Expected: no new failures compared with the recorded baseline. Any pre-existing order-dependent discovery failure must be reported separately and not hidden.

- [ ] **Step 5: Commit Task 5**

```bash
git add .env.example README.md CHANGELOG.md docs/plans/2026-08-21-modular-feature-control-design.md
git commit -m "docs: explain modular runtime controls"
```

- [ ] **Step 6: Inspect final branch**

Run:

```bash
git status --short --branch
git log --oneline --decorate -7
git diff 30daea6...HEAD --stat
```

Expected: clean feature branch containing only the approved modular-control work.
