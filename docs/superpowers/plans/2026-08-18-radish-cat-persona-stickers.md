# Radish Cat Persona And Stickers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the approved 萝卜猫 persona, direct-address casual fallback, and weighted optional stickers without changing business replies.

**Architecture:** Keep business routing authoritative and delegate only its `unknown` intent to the existing random-chat reply path. Isolate sticker discovery and weighting in a pure helper so probability behavior is deterministic under tests, while runtime assets remain under ignored server data.

**Tech Stack:** Python 3.10, NoneBot2, OneBot V11, unittest, systemd, Git.

---

### Task 1: Configuration And Sticker Selection

**Files:**
- Modify: `plugins/violation_record/config.py`
- Create: `plugins/random_chat/stickers.py`
- Modify: `.env.example`
- Test: `tests/test_random_chat_stickers.py`

- [ ] Write failing tests for the 20% attachment gate, the special image's conditional 10% bucket, normal-image selection, and missing-directory fallback.
- [ ] Run `.venv/bin/python -m unittest tests.test_random_chat_stickers -v` and confirm failure because the sticker module and configuration fields do not exist.
- [ ] Implement bounded configuration values, a safe-default direct-fallback switch, plus a non-recursive supported-extension file scan and deterministic two-stage selector.
- [ ] Re-run the focused test and confirm all cases pass.

### Task 2: Persona And Direct Reply Semantics

**Files:**
- Modify: `plugins/random_chat/ai.py`
- Test: `tests/test_random_chat.py`

- [ ] Add failing assertions for 萝卜猫, flowers/plants, the occasional 反二梦女 joke, restrained natural speech, and direct-address mode that must answer rather than emit `SKIP`.
- [ ] Run `.venv/bin/python -m unittest tests.test_random_chat -v` and confirm the new assertions fail.
- [ ] Add an `addressed` argument and compose the smallest prompt branch that preserves current spontaneous-chat behavior.
- [ ] Re-run the focused test and confirm it passes.

### Task 3: Message Composition And Business Fallback

**Files:**
- Modify: `plugins/random_chat/matcher.py`
- Modify: `plugins/violation_record/matcher.py`
- Test: `tests/test_random_chat_context.py`
- Test: `tests/test_violation_matcher.py`

- [ ] Add failing tests proving a selected sticker is appended to the same message, no sticker keeps a text-only send, enabled `unknown` direct messages delegate to random chat, the disabled switch preserves the old business response, and known business intents never delegate.
- [ ] Run both focused test modules and verify the failures are caused by missing behavior.
- [ ] Make `send_random_reply` accept direct mode, return whether it sent, and append at most one local image; route only parsed `unknown` intents from the business matcher.
- [ ] Re-run the focused tests and confirm they pass.

### Task 4: Release, Deployment, And Verification

**Files:**
- Modify: `.env` on the server only
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Create: `docs/releases/2026-08-18-v1.0.2.6beta.md`

- [ ] Set production `RANDOM_CHAT_PROBABILITY=0.03`, `RANDOM_CHAT_STICKER_PROBABILITY=0.20`, and the special filename; do not commit `.env` or sticker assets.
- [ ] Document persona, routing precedence, probability semantics, storage path, and rollback.
- [ ] Run the full unittest suite and Python compilation checks.
- [ ] Restart `qq-violation-bot`, verify it is active, inspect fresh logs, and confirm OneBot account `2727968581` is connected.
- [ ] Review `git diff`, remove the unrelated stray memory README sentence, commit the intended code/docs as `v1.0.2.6beta`, tag, and push the feature branch and tag.
