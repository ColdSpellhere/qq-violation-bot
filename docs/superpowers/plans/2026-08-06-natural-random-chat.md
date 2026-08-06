# Natural Random Chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make random replies context-sensitive, optional, concise, and free of repetitive stock openings.

**Architecture:** Keep generation in `plugins/random_chat/ai.py` and expose no new runtime configuration. Normalize the model result in the existing response boundary so `None` continues to suppress delivery in the matcher.

**Tech Stack:** Python 3.10, `httpx`, `unittest`, NoneBot2.

---

### Task 1: Specify natural-response behavior

**Files:**
- Modify: `tests/test_random_chat.py`

- [ ] Add assertions that the system prompt defines a real QQ group participant, permits exact `SKIP`, discourages fixed openings, and asks for only the final message.
- [ ] Add test cases proving `SKIP` and ` 哈哈，...` return `None`, while ordinary content remains unchanged.
- [ ] Run `.venv/bin/python -m unittest tests.test_random_chat -v` and confirm the new assertions fail for the missing behavior.

### Task 2: Implement minimal normalization

**Files:**
- Modify: `plugins/random_chat/ai.py`
- Test: `tests/test_random_chat.py`

- [ ] Replace only the system prompt; retain the current request shape, context ordering, timeout, model, and temperature.
- [ ] Add a small pure result normalizer that returns `None` for empty output, exact case-insensitive `SKIP`, and the repetitive leading form `哈哈，` or `哈哈,`.
- [ ] Run `.venv/bin/python -m unittest tests.test_random_chat -v` and confirm all focused tests pass.

### Task 3: Verify and deploy

**Files:**
- Verify: repository-wide tests and service logs

- [ ] Run `TARGET_GROUP_ID=246813579 .venv/bin/python -m unittest discover -s tests -t . -v` and require zero failures.
- [ ] Inspect `git diff --check` and `git diff -- plugins/random_chat/ai.py tests/test_random_chat.py`.
- [ ] Commit the focused change on `feat/context-aware-random-chat` without creating a tag.
- [ ] Restart the existing bot service only, then confirm the service is active and the OneBot connection is established in fresh logs.
