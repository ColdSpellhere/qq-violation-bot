# Classified Managed Keyword Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single private managed-keyword file with a scalable, AI-readable seven-category catalog while preserving manual QQ governance and applying strict redaction only to the China-political category.

**Architecture:** Keep `keywords.json` as the only QQ-visible manual store. Add an instance-private `managed/` catalog whose atomic `current.json` pointer selects one immutable generation containing a self-describing manifest and category shards. The runtime compiles one cached snapshot per generation, retains the last-known-good snapshot on malformed updates, and applies the strictest disclosure policy across all matches in one message.

**Tech Stack:** Python 3.10+, NoneBot2, OneBot V11, JSON, SHA-256, atomic filesystem replacement, unittest/IsolatedAsyncioTestCase.

---

### Task 1: Lock the catalog and disclosure contracts with RED tests

**Files:**
- Create: `tests/test_content_alert_catalog.py`
- Modify: `tests/test_hive_keyword_alert.py`

- [ ] **Step 1: Define synthetic seven-category fixtures**

Create private test generations with the stable category IDs `political_cn`, `sexual_explicit`, `gender_conflict`, `controversial_topics`, `anime_game_controversy`, `graphic_violence`, and `terrorism`. Use only placeholder terms. Each category descriptor includes `name_zh`, `description`, `severity`, `disclosure_policy`, version, source references, and one or more hashed shards.

- [ ] **Step 2: Assert atomic loading and last-known-good behavior**

Assert that a valid generation loads all enabled shards, a pointer switch atomically changes the generation, and a missing, malformed, hash-mismatched, path-traversing, symlinked, duplicate, or over-limit shard leaves the complete previous generation active. Cold-start failure must expose an empty snapshot, never a partial catalog.

- [ ] **Step 3: Assert classification and strictest-policy behavior**

Assert `political_cn` cannot be downgraded from `strict_hidden`; ordinary managed categories use `management_visible`; duplicate normalized terms merge their category provenance; and any political match makes the complete event strict-hidden, including simultaneous manual and ordinary matches.

- [ ] **Step 4: Assert QQ isolation and bounded output**

Assert the real QQ matcher wiring gives commands only the manual store. `/违禁词 列表`, help, status, add, delete, and errors must not expose catalog category metadata, counts, IDs, paths, or terms. Ordinary alerts may show category labels, unique terms, nickname, and a bounded excerpt; strict alerts must hide all term/category/internal metadata, nickname, and message content. Both remain a single text segment with a hard report-length limit.

- [ ] **Step 5: Assert scale and compilation caching**

Load 10,000 synthetic entries across multiple shards, match a tail entry, and assert unchanged `current.json` does not reread/recompile shards on every event.

- [ ] **Step 6: Verify RED**

Run `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest tests.test_content_alert_catalog tests.test_hive_keyword_alert -v`. Require failures caused by the missing catalog and per-category disclosure implementation.

### Task 2: Implement the private generation catalog

**Files:**
- Create: `plugins/content_alert/catalog.py`
- Modify: `plugins/violation_record/config.py`
- Modify: `plugins/content_alert/engine.py`

- [ ] **Step 1: Add a fixed instance-private catalog root**

Derive `content_alert_managed_catalog_path` from `BOT_INSTANCE_ROOT/data/content_alert/managed/current.json`; no environment variable may redirect it outside the instance root.

- [ ] **Step 2: Decode and validate the AI-readable schema**

Implement immutable category, source, entry, and snapshot records. Validate stable IDs, enums, dates, status, rule and alias length, per-shard and total limits, SHA-256, relative paths, generation consistency, normalized duplicates, file ownership-safe path boundaries, and absence of symbolic links.

- [ ] **Step 3: Make political disclosure non-downgradable**

Enforce `political_cn -> strict_hidden` in code. Reject a generation that declares any other policy for that category. Unknown categories may load only with a valid explicit policy and remain private from QQ governance.

- [ ] **Step 4: Cache and atomically publish snapshots**

Check the small pointer fingerprint on each snapshot request. Only when it changes, read and verify the complete immutable generation, compile matchers, then replace the snapshot under a lock. Keep the last-known-good generation on every refresh error and expose only a server-side error type/generation identifier.

- [ ] **Step 5: Scale the literal matcher without changing manual limits**

Allow the managed catalog to compile up to 50,000 validated patterns while retaining the 200-rule manual command limit. Cache the compiled managed matcher; preserve NFKC/casefold/whitespace/Cf normalization and text-segment boundaries.

- [ ] **Step 6: Verify GREEN**

Run the catalog tests and existing engine/store tests. Require all assertions and `git diff --check` to pass.

### Task 3: Integrate category-aware alerts without widening QQ commands

**Files:**
- Modify: `plugins/content_alert/service.py`
- Modify: `plugins/content_alert/matcher.py`
- Modify: `tests/test_hive_keyword_alert.py`

- [ ] **Step 1: Wire the catalog only into the alert path**

Instantiate `ManagedKeywordCatalog` only for `ContentAlertService`. Keep `RULE_STORE` as the sole dependency of QQ keyword commands. Retain `background_keywords.json` as a strict-hidden fallback only when no valid managed generation exists.

- [ ] **Step 2: Apply event-level disclosure policy**

For `management_visible` matches, show safe category labels, a bounded unique term list, the sanitized nickname, and the existing bounded excerpt. If any strict match exists, hide every manual/managed term, every category, nickname, and all message segments behind constant placeholders. Never expose internal IDs, shard/generation/hash/source/count metadata in QQ output.

- [ ] **Step 3: Preserve operational boundaries**

Keep alerts non-enforcing, one plain-text OneBot segment, deduplicated per event, independent of AI, and subject to the existing content-alert runtime switch and source/report group isolation.

- [ ] **Step 4: Verify GREEN and regressions**

Run catalog, content-alert, feature-control, plugin-load, and public-scanner tests before the complete suite.

### Task 4: Add a safe backend importer and documentation

**Files:**
- Create: `scripts/import_content_alert_catalog.py`
- Create: `tests/test_import_content_alert_catalog.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Implement deterministic private generation import**

Accept a private build document and an instance root. Validate before writing, acquire an instance-local lock, write a new immutable generation with mode `0600` under `0700` directories, fsync files/directories, compute shard hashes, verify by loading it, then atomically replace `current.json`. The same input hash must be a no-op; conflicting generation contents must abort.

- [ ] **Step 2: Protect legacy migration and rollback**

Back up the current legacy file and pointer before activation. Import the existing 60 legacy rules into `political_cn` without altering the legacy file. Rollback changes only `current.json` or removes the managed pointer; legacy fallback remains strict-hidden.

- [ ] **Step 3: Document schema and safety without rule content**

Document category IDs, disclosure policies, private paths, import/rollback/health commands, AI-consumption fields, and that no real terms belong in Git, docs, tests, logs, QQ commands, or deployment output.

- [ ] **Step 4: Verify importer tests and public-source rejection**

Assert modes, hashes, idempotence, fail-closed behavior, no symlinks, no half-generation publication, and that every tracked `data/content_alert/` artifact is rejected by the public scanner.

### Task 5: Build and deploy the large private catalog

**Files:**
- Runtime-only: `/opt/qq-bots/instances/carrot/data/content_alert/managed/`
- Runtime-only: `/opt/qq-bots/instances/carrot/backups/content-alert/`

- [ ] **Step 1: Collect licensed historical baselines and recent additions**

Use pinned, licensed public lexicons for political, explicit-sexual, gender-conflict, violence, and terrorism baselines. Add current official leadership names and vetted historical/current event aliases to `political_cn`. Add recent, source-dated gender, controversy, anime, and game-community entries from current web research. Record source URL, license, retrieval time, source revision/hash, confidence, first-seen, last-reviewed, and tags in the private build document.

- [ ] **Step 2: Expand variants privately**

For protected political entities/events, attach exact forms, established euphemisms, abbreviations, pinyin/initial forms, homophones, and split-character aliases to one canonical entry. Rely on runtime normalization for whitespace, full-width, case, and formatting characters instead of duplicating those forms. Do not print or commit the resulting values.

- [ ] **Step 3: Quality gates**

Remove normalized duplicates, empty/control-only entries, overlong values, exact allowlisted benign collisions, and unsupported binary/URL material. Produce counts by category/source/status only. The catalog is detection-only and must not modify business records, delete messages, mute users, or call an LLM.

- [ ] **Step 4: Guarded CArroT rollout**

Create a timestamped backup, tighten the content-alert backup parent to `0700`, deploy one immutable CArroT release, disable the runtime alert switch only for the pointer transition, import and validate the catalog, restart only CArroT, restore the switch, and verify OneBot identity, reverse WebSocket, target-group membership, current SHA, permissions, and post-start logs. Leave Kona unchanged.

- [ ] **Step 5: Offline acceptance without QQ publication**

Use a fake Bot and real private catalog entries inside the server process to verify all seven categories, visible-category formatting, strict political redaction, mixed-policy precedence, manual-only `/违禁词 列表`, and zero network calls. Output only pass/fail and counts; never output terms or message bodies.

- [ ] **Step 6: Final release gates**

Run the full test suite, compilation, public tree/history scans, Git diff/privacy review, catalog integrity and count checks, service health, and rollback-pointer validation. Commit and fast-forward GitHub `main` only after CArroT passes; do not deploy Kona.

Rollback: disable the alert runtime switch, atomically point `current.json` to the previous valid generation (or remove the pointer to use the unchanged strict legacy fallback), switch CArroT to its previous immutable release if needed, restart only `qqbot@carrot`, restore the switch, and repeat offline redaction plus service/OneBot checks.
