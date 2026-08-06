# v1.0.2.3beta Member Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add relationship-aware context and conservative persistent member memory to 5% random group replies.

**Architecture:** Extend archived context with stable QQ identities and message targets. Store validated member profiles in an additive SQLite table with an ignored JSON mirror, and run best-effort extraction only after triggered replies.

**Tech Stack:** Python 3.10, SQLite, `httpx`, NoneBot2, `unittest`.

---

### Task 1: Relationship-aware context

**Files:**
- Modify: `plugins/chat_archive/db.py`
- Modify: `plugins/random_chat/ai.py`
- Test: `tests/test_random_chat_context.py`
- Test: `tests/test_random_chat.py`

- [ ] Add failing tests for sender QQ, mentions, reply target resolution, and prompt rendering.
- [ ] Run focused tests and confirm failures are caused by missing metadata.
- [ ] Extend `ContextMessage` and the context SQL without changing the 30-minute/20-message bounds.
- [ ] Render explicit speaker and target labels; instruct the model not to treat member-to-member speech as addressed to itself.
- [ ] Run focused tests and require zero failures.

### Task 2: Conservative member store

**Files:**
- Create: `plugins/member_memory/__init__.py`
- Create: `plugins/member_memory/store.py`
- Create: `tests/test_member_memory.py`
- Modify: `plugins/chat_archive/matcher.py`
- Modify: `.gitignore`
- Create: `data/member_memory/.gitignore`
- Create: `data/member_memory/README.md`

- [ ] Add failing tests for identity aliases, bounded traits, evidence replacement, atomic JSON mirror, and ignored runtime profiles.
- [ ] Implement additive schema and validated profile operations.
- [ ] Update identity only after a successful archive write.
- [ ] Run memory and archive tests and require zero failures.

### Task 3: Extraction and integration

**Files:**
- Create: `plugins/member_memory/ai.py`
- Modify: `plugins/random_chat/matcher.py`
- Modify: `plugins/random_chat/ai.py`
- Modify: `plugins/violation_record/config.py`
- Modify: `.env.example`
- Test: `tests/test_random_chat_context.py`
- Test: `tests/test_member_memory.py`

- [ ] Add failing tests for profile injection, conservative JSON extraction, malformed-output fallback, and best-effort persistence after reply delivery.
- [ ] Implement strict extraction validation and relevant-profile loading.
- [ ] Keep extraction failure isolated from sending and all business matchers.
- [ ] Run focused tests and require zero failures.

### Task 4: Release and deployment

**Files:**
- Modify: `CHANGELOG.md`
- Create: `docs/releases/2026-08-06-v1.0.2.3beta.md`

- [ ] Run the complete suite with a synthetic `TARGET_GROUP_ID` and require zero failures.
- [ ] Run `git diff --check`, public-source safety tests, and inspect the complete diff.
- [ ] Back up production `chat_archive.db`, commit the release, and tag `v1.0.2.3beta`.
- [ ] Push the branch and tag to GitHub without committing runtime member data.
- [ ] Restart only NoneBot and verify both services plus the OneBot WebSocket connection.
