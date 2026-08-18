# Member Query Routing Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent fuzzy member queries from falling back to a 20-record area query.

**Architecture:** Add a deterministic parser for the documented `查询 <群> <名字>` shape before the existing area-query shortcut. Keep member resolution and its not-found/ambiguous responses unchanged.

**Tech Stack:** Python 3, unittest, NoneBot plugin code.

---

### Task 1: Lock the query contract

**Files:**
- Modify: `tests/test_query_contract.py`

- [ ] Add tests showing member names are retained with and without the `违规记录` suffix.
- [ ] Add an integration assertion showing an unmatched target returns the existing one-line member error.
- [ ] Run `TARGET_GROUP_ID=123456789 .venv/bin/python -m unittest tests.test_query_contract -v` and confirm the new assertions fail against the old router.

### Task 2: Implement the narrow routing fix

**Files:**
- Modify: `plugins/violation_record/ai_router.py`

- [ ] Parse a query verb, group area and non-empty member token before the area-only shortcut.
- [ ] Remove only recognized record/time suffixes and map a numeric target to `qq_number`.
- [ ] Run the focused query contract and confirm it passes.
- [ ] Run the full unittest suite and confirm no existing area-query behavior regresses.

### Task 3: Record and deploy the beta fix

**Files:**
- Modify: `CHANGELOG.md`
- Create: `docs/releases/2026-08-18-v1.0.2.4beta.md`

- [ ] Record the bug fix, compatibility boundary and rollback steps as `v1.0.2.4beta`.
- [ ] Back up only the remote files being replaced.
- [ ] Deploy the focused files, restart `qq-violation-bot.service`, and check service and recent logs.
