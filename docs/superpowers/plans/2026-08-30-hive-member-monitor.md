# Hive Member Monitor Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a CArroT-only, AI-free monitor for one QQ group that exports the first full member list to a separate report group and reports departures there without allowing monitored messages into chat, archive, vision, memory, business, or heartbeat paths.

**Architecture:** Add an opt-in `plugins.hive_member_monitor` plugin backed by an instance-local SQLite database. A configuration-level hard exclusion prevents monitor-only groups from becoming chat candidates even if somebody later adds them to the runtime chat allowlist. The plugin uses OneBot V11 member-list and group notice APIs only; it contains no LLM dependency. Startup performs an idempotent full reconciliation, periodic reconciliation catches notices missed while offline, and real-time increase/decrease notices keep the snapshot current.

**Tech Stack:** Python 3.10+, NoneBot2, OneBot V11, SQLite, openpyxl, unittest/IsolatedAsyncioTestCase.

---

### Task 1: Lock the monitor-only isolation boundary

**Files:**
- Modify: `plugins/violation_record/config.py`
- Modify: `plugins/feature_control/state.py`
- Modify: `plugins/feature_control/runtime.py`
- Test: `tests/test_hive_member_monitor.py`
- Test: `tests/test_instance_config.py`

**Step 1: Write failing tests**

- Configure a fake monitor-only group and also put it in the runtime group-chat allowlist.
- Assert `FeatureController.group_chat_allowed()` returns false for the monitor-only group while normal allowed groups remain true.
- Assert monitor database and export paths resolve below `BOT_INSTANCE_ROOT` and differ across two instances.

**Step 2: Run tests and verify RED**

Run: `.venv/bin/python -B -m unittest tests.test_hive_member_monitor tests.test_instance_config`

Expected: failures because monitor-only configuration and exclusions do not exist.

**Step 3: Implement the minimum configuration contract**

Add default-off environment fields:

```python
hive_member_monitor_enabled: bool
hive_member_monitor_group_id: int
hive_member_report_group_id: int
hive_member_monitor_reconcile_seconds: int
hive_member_monitor_database_path: Path
hive_member_monitor_export_dir: Path
```

Pass every configured monitor-only group to `FeatureController` even while monitoring is runtime-disabled, and make this exclusion take precedence over every persisted allowlist value. Feature-control and memory-governance matchers must also reject these groups, and startup must reject a monitor group equal to the business target.

**Step 4: Run tests and verify GREEN**

Run the same command and require exit code 0.

### Task 2: Persist an idempotent member snapshot and export state

**Files:**
- Create: `plugins/hive_member_monitor/__init__.py`
- Create: `plugins/hive_member_monitor/store.py`
- Create: `plugins/hive_member_monitor/exporter.py`
- Test: `tests/test_hive_member_monitor.py`

**Step 1: Write failing store/export tests**

- First valid member list is normalized and persisted atomically.
- QQ name precedence is group card, nickname, then QQ number; duplicate user IDs are removed.
- Invalid/non-list/empty API payloads never erase an existing snapshot.
- The workbook contains exactly `QQ号` and `QQ名字`, keeps QQ IDs as text, neutralizes formula prefixes and illegal control characters in untrusted names, uses a stable order, and has the filename `蜂巢群员名单_YYYY-MM-DD_HH-mm-ss.xlsx`.
- `initial_export_delivered` survives a new store instance.

**Step 2: Run the focused test and verify RED**

Run: `.venv/bin/python -B -m unittest tests.test_hive_member_monitor`

**Step 3: Implement schema and exporter**

Use idempotent SQLite tables for metadata, current members, pending departures, delivered event keys, leases, and export target/hash audit data. Avoid SQL statements whose variable count grows with a 2,894-member list. Use transactions and WAL-compatible connections. Do not store chat text or invoke any AI component.

**Step 4: Run the focused test and verify GREEN**

Run the same command and require exit code 0.

### Task 3: Implement first delivery and reliable departure monitoring

**Files:**
- Create: `plugins/hive_member_monitor/service.py`
- Create: `plugins/hive_member_monitor/matcher.py`
- Create: `plugins/hive_member_monitor/lifecycle.py`
- Modify: `plugins/hive_member_monitor/__init__.py`
- Test: `tests/test_hive_member_monitor.py`

**Step 1: Write failing service tests**

- First successful member fetch uploads one workbook to the report group; a restart does not upload it twice.
- Upload failure leaves the delivery pending and retries later/restart.
- A matching `GroupDecreaseNoticeEvent` sends one structured log with monitored group, QQ, name, departure type, operator, event time, and OneBot source.
- Duplicate events are idempotent across restart; concurrent workers atomically lease outbox rows; failed sends are retryable.
- A delayed notice and an earlier reconciliation event for the same membership episode are merged, while replaying an old delivered event after a rejoin cannot mark the member inactive.
- Other groups and `kick_me` for the bot itself are ignored as member-departure logs.
- Group increase updates the snapshot silently.
- Initial export and reconciliation require the normalized list count to equal `get_group_info.member_count`.
- Reconciliation requires two consecutive valid absences before producing a synthetic missed-departure log, applies an absolute/ratio mass-departure fuse, caps each pending delivery batch, and never interprets an invalid/empty/truncated response as all members leaving.

**Step 2: Run the focused test and verify RED**

Run: `.venv/bin/python -B -m unittest tests.test_hive_member_monitor`

**Step 3: Implement service, matchers, and bounded lifecycle**

- Register notice matchers only when enabled.
- On startup, initialize schema, retry pending delivery/events, and perform a valid full fetch.
- Run one cancellable reconcile task with a configurable interval.
- On shutdown, cancel and await the task.
- Mark an export or event delivered only after the OneBot call succeeds; document the unavoidable remote-success/local-crash at-least-once window and include stable event IDs for audit.
- Commit member departure state and its outbox row in one SQLite transaction.  Lease one notification at a time for ten minutes, and reject delayed events older than the current membership episode start; ordinary full-list refreshes must not move that episode watermark.
- Treat a difference above the 2%/20-member fuse as a persisted candidate: it may advance only when the complete member-ID set is identical, and is accepted automatically on the third observation so a legitimate large offline change cannot remain fused forever.

**Step 4: Run the focused test and verify GREEN**

Run the same command and require exit code 0.

### Task 4: Register the plugin without affecting Kona

**Files:**
- Modify: `bot.py`
- Modify: `.env.example`
- Modify: `tests/test_plugin_loading.py`
- Modify: `tests/test_group_router.py`

**Step 1: Write failing integration tests**

- Enabled CArroT-like environment loads the plugin before chat handlers.
- Disabled/Kona-like environment registers no monitor matcher, lifecycle worker, database, or member API call.
- A monitor-only group cannot enter business/chat/archive/member-memory/vision code even when mistakenly present in the chat allowlist.

**Step 2: Run tests and verify RED**

Run: `.venv/bin/python -B -m unittest tests.test_plugin_loading tests.test_group_router tests.test_hive_member_monitor`

**Step 3: Add conditional registration and documented placeholders**

Load `plugins.hive_member_monitor` from `bot.py`; the plugin itself must remain inert unless `HIVE_MEMBER_MONITOR_ENABLED=true`. Add placeholder-only settings to `.env.example`; never commit real QQ IDs.

**Step 4: Run tests and verify GREEN**

Run the same command and require exit code 0.

### Task 5: Verify, scan, and prepare guarded CArroT deployment

**Files:**
- Inspect: all changed files
- Runtime-only modify after explicit Git/deploy authorization: `/opt/qq-bots/instances/carrot/.env`

**Step 1: Run focused and full verification**

```bash
.venv/bin/python -B -m unittest tests.test_hive_member_monitor tests.test_group_router tests.test_plugin_loading tests.test_instance_config
.venv/bin/python -B -m unittest discover -s tests
```

**Step 2: Scan public source and diff**

- Run the repository public-source scanner.
- Verify no real QQ IDs, secrets, databases, exports, logs, or runtime paths are tracked.
- Inspect `git diff --check`, `git status`, and the full diff.

**Step 3: Stop at the publishing boundary**

Because the repository instructions require explicit authorization for commit/push/deployment, present the tested diff and request the single confirmation needed to create an immutable CArroT release. Kona remains unchanged.

**Step 4: After authorization, deploy and behaviorally verify**

- Back up CArroT state and `.env`.
- Configure the real monitor/report group IDs only in CArroT's private instance `.env`.
- Commit, build an immutable release, deploy CArroT only, and retain the previous symlink for rollback.
- Verify OneBot login, plugin startup, member count, SQLite snapshot, exact Excel filename/upload success, exclusion from chat/archive/AI, and absence from heartbeat targets.
- Do not manufacture a departure event; report that live departure delivery remains acceptance-tested only when a real member leaves, unless the user authorizes a controlled test.
