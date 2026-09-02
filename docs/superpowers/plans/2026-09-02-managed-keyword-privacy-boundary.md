# Managed Keyword Privacy Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep server-managed keyword rules effective for detection while preventing their text from appearing in any QQ command reply or alert message.

**Architecture:** Keep the existing instance-local `keywords.json` exclusively for QQ-managed rules and add a physically separate `background_keywords.json` that is never passed to QQ commands. The alert service matches both stores independently so colliding rule IDs cannot merge the sources. Its formatter treats every background hit as sensitive and replaces rule metadata, message excerpts, and sender display names with fixed placeholders.

**Tech Stack:** Python 3.10+, NoneBot2, OneBot V11, JSON, unittest/IsolatedAsyncioTestCase.

---

### Task 1: Lock the privacy contract with failing tests

**Files:**
- Modify: `tests/test_hive_keyword_alert.py`

- [ ] **Step 1: Add rule-store isolation assertions**

Create independent `keywords.json` and `background_keywords.json` stores. Assert that QQ commands receive only the manual store, list only manual rules, cannot delete a background identifier, and can add the same literal without learning whether it exists in the background store.

- [ ] **Step 2: Add QQ-output non-disclosure assertions**

Assert `/违禁词 列表` contains the manual rule but not the managed rule. Assert guessed managed-rule deletion fails without including its pattern. Send a message and nickname containing a managed rule, then assert the alert still fires but contains neither the managed pattern nor the raw message/nickname; the report must say that server-managed details were hidden.

- [ ] **Step 3: Verify RED**

Run `python -B -m unittest tests.test_hive_keyword_alert -v`. The new tests must fail because rule origins, batch import, manual-only snapshots, and managed-output redaction do not yet exist.

### Task 2: Add the physically separate background store

**Files:**
- Modify: `plugins/violation_record/config.py`
- Modify: `plugins/content_alert/matcher.py`

- [ ] **Step 1: Add the instance-private path**

Derive `content_alert_background_rules_path` as `BOT_INSTANCE_ROOT/data/content_alert/background_keywords.json`; it is not configurable through a public environment value and never belongs to the immutable release.

- [ ] **Step 2: Keep governance ignorant of the background store**

Instantiate `RULE_STORE` for commands exactly as before and instantiate a separate `BACKGROUND_RULE_STORE` only for alert matching. Never pass the background store to command parsing, list, add, delete, help, or module-status code.

- [ ] **Step 3: Verify storage GREEN**

Run the focused store and command tests and require exit code 0.

### Task 3: Match both stores and block QQ disclosure

**Files:**
- Modify: `plugins/content_alert/service.py`
- Modify: `plugins/content_alert/matcher.py`
- Test: `tests/test_hive_keyword_alert.py`

- [ ] **Step 1: Match stores independently**

Add an optional `background_rule_store` to `ContentAlertService`. Build separate literal matchers and keep separate manual/background match tuples so duplicate IDs in the two files cannot collide or suppress each other.

- [ ] **Step 2: Preserve the command contract**

Leave `/违禁词 列表/添加/删除` bound only to the existing manual `RULE_STORE`. A background rule cannot affect command success, error text, or visible identifiers.

- [ ] **Step 3: Apply a fail-closed report formatter**

When any background rule matches, omit the raw excerpt and sender nickname. Show only a fixed phrase such as `后台受控规则命中（详情仅服务器可见）`; never include background counts, IDs, hashes, paths, or patterns. Preserve the existing manual-rule report format when no background content is present; mixed hits may show only their manual-rule details.

- [ ] **Step 4: Verify GREEN and regressions**

Run `python -B -m unittest tests.test_hive_keyword_alert tests.test_plugin_loading -v`, then the complete suite.

### Task 4: Document, scan, deploy, and import privately

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Runtime-only: `/opt/qq-bots/instances/carrot/data/content_alert/keywords.json`

- [ ] **Step 1: Document the boundary without publishing rule data**

Document that QQ commands manage only group-added rules, managed rules are server-only, and managed hits hide the pattern, nickname, and excerpt. Do not add real keywords to Git, tests, logs, command examples, or documentation.

- [ ] **Step 2: Run release gates**

Run focused/full tests, `git diff --check`, public tree/history scans, and inspect the tracked diff for runtime data or secrets.

- [ ] **Step 3: Guarded CArroT rollout**

Back up the existing private rule file, deploy one immutable CArroT candidate, and verify service, release manifest, plugin loading, OneBot identity, reverse WebSocket, and rule-file permissions. Leave Kona unchanged because the content-alert capability is not configured there.

- [ ] **Step 4: Import without QQ output and verify behavior offline**

Temporarily disable the alert runtime switch, atomically import the batch as `managed`, verify only counts/origins and file mode in the server shell, then restore the switch. Use synthetic in-process tests—not a real sensitive group message—to prove a managed hit produces a redacted alert and that `/违禁词 列表` returns only manual rules.

Rollback: restore the prior CArroT release pointer and the timestamped pre-import rule-file backup together, then restart only `qqbot@carrot` and rerun instance health.
