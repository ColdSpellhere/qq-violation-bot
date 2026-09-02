# Hive Keyword Alert Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an AI-free, CArroT-only literal-keyword alert for real-time text messages in the privately configured Hive source group, with alerts delivered to the configured management group.

**Architecture:** Add an opt-in `plugins.content_alert` plugin with a small literal matching engine, an instance-local atomically written rule file, a superuser-only rule command handler, and a real-time group message matcher. Reuse the existing feature controller for a runtime kill switch and keep the source group behind the existing `monitor_only` boundary so its messages never enter chat, archive, vision, memory, or business paths. The generic plugin boundary permits later regex, algorithmic, or AI detectors without changing the OneBot message ingress.

**Tech Stack:** Python 3.10+, NoneBot2, OneBot V11, JSON, unittest/IsolatedAsyncioTestCase.

---

### Task 1: Lock configuration and runtime-switch boundaries

**Files:**
- Modify: `plugins/violation_record/config.py`
- Modify: `plugins/feature_control/state.py`
- Modify: `plugins/feature_control/runtime.py`
- Modify: `plugins/feature_control/commands.py`
- Test: `tests/test_hive_keyword_alert.py`
- Test: `tests/test_feature_control_commands.py`

- [ ] **Step 1: Write failing configuration and switch tests**

Assert that source groups, report group, and rule path are instance-scoped; capability requires positive distinct IDs and every source group to be `monitor_only`; persisted feature state safely defaults the new switch; `/违禁词告警 开|关` requires capability and `/模块状态` reports it.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -B -m unittest tests.test_hive_keyword_alert tests.test_feature_control_commands`

Expected: failures because the keyword-alert configuration and runtime switch do not exist.

- [ ] **Step 3: Implement minimal configuration and switch support**

Add placeholder-only environment fields `CONTENT_ALERT_ENABLED`, `CONTENT_ALERT_SOURCE_GROUP_IDS`, and `CONTENT_ALERT_REPORT_GROUP_ID`. Keep the rule file fixed below `BOT_INSTANCE_ROOT/data/content_alert/keywords.json`; no real group IDs or keywords enter Git.

- [ ] **Step 4: Run tests and verify GREEN**

Run the same focused command and require exit code 0.

### Task 2: Implement the literal rule store and matcher

**Files:**
- Create: `plugins/content_alert/__init__.py`
- Create: `plugins/content_alert/rules.py`
- Create: `plugins/content_alert/engine.py`
- Test: `tests/test_hive_keyword_alert.py`

- [ ] **Step 1: Write failing engine and storage tests**

Cover NFKC plus case-folded literal matching, duplicate suppression, no matching across non-text segments, stable rule IDs, invalid/control-character rejection, a 200-rule/64-character bound, atomic updates, instance isolation, `0700` directory and `0600` file modes, and safe reload after external replacement.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -B -m unittest tests.test_hive_keyword_alert`

Expected: import or assertion failures because the rule store and engine are absent.

- [ ] **Step 3: Implement the minimal engine and rule store**

Store only rule metadata, never chat content. Use schema version 1 with literal rules only. Reject symbolic-link targets, write through a same-directory temporary file, `fsync`, set mode `0600`, and replace atomically. Reload on file modification so manual edits are hot-applied.

- [ ] **Step 4: Run tests and verify GREEN**

Run the same focused command and require exit code 0.

### Task 3: Add superuser governance and alert delivery

**Files:**
- Create: `plugins/content_alert/commands.py`
- Create: `plugins/content_alert/matcher.py`
- Create: `plugins/content_alert/content_alert_runtime.py`
- Test: `tests/test_hive_keyword_alert.py`
- Modify: `tests/test_plugin_loading.py`

- [ ] **Step 1: Write failing command and delivery tests**

Cover `/违禁词 列表`, `/违禁词 添加 <词>`, and `/违禁词 删除 <编号>` for superusers only, usable through private chat or an addressed management-group message but never in the source group. Alert only real-time source-group `text` segments from non-self users; include source, sender, time, matched rules, bounded excerpt, message ID, and the explicit “literal matching/no AI/no automatic punishment” boundary. Verify OneBot receives a text segment, duplicate in-process deliveries send once, and send failures are reported as failures rather than success.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -B -m unittest tests.test_hive_keyword_alert tests.test_plugin_loading`

Expected: failures because command and message matchers are not registered.

- [ ] **Step 3: Implement command, matcher, and conditional runtime registration**

Register only when static private configuration is complete and enabled. Use priority 1, `block=False` for passive source monitoring and a separate priority-0 `block=True` superuser command matcher. Give the runtime entrypoint a unique module name so it cannot collide with the existing model-gateway runtime plugin. Send alerts with `MessageSegment.text` to prevent CQ injection. Keep a bounded in-process delivered-message guard; do not create a chat archive or persist message text.

- [ ] **Step 4: Run tests and verify GREEN**

Run the same focused command and require exit code 0.

### Task 4: Document, scan, and deploy CArroT safely

**Files:**
- Modify: `bot.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `scripts/check_public_tree.py`
- Modify: `tests/test_public_source.py`

- [ ] **Step 1: Add failing registration and public-boundary tests**

Assert configured CArroT loads the alert plugin before chat handlers, an unconfigured/Kona-like instance registers nothing, the tracked tree contains no real rules/group IDs, and the plugin has no LLM dependency.

- [ ] **Step 2: Implement registration and operator documentation**

Document the match semantics, command syntax, runtime switch, privacy behavior, known MVP limits, and later engine extension point. Add only blank/example values to `.env.example`.

- [ ] **Step 3: Run focused and full verification**

Run focused tests, the complete test suite, `git diff --check`, current-tree and history public scans, and inspect the final diff/status.

- [ ] **Step 4: Guarded production rollout**

Back up CArroT `.env`, `runtime_features.json`, and any existing private rule file; configure only the Hive source and management report group in CArroT; deploy one immutable release; leave Kona unchanged. Verify service/plugin/OneBot identity, runtime switch, private file permissions, group membership, no LLM call path, and no post-start errors. Do not manufacture a user violation; use unit/integration tests until a natural or user-authorized controlled message occurs.
