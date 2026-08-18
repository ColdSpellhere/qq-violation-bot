# Merged Query Forward Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send multi-record query results as one merged-forward card without losing per-record evidence images.

**Architecture:** Keep the service-layer `StructuredReply` contract unchanged and modify only the NoneBot delivery helper. Build one custom forward node per record; on forward API failure, send one combined text fallback.

**Tech Stack:** Python 3, NoneBot OneBot V11, NapCat, unittest.

---

### Task 1: Lock delivery behavior with failing tests

**Files:**
- Modify: `tests/test_reply_delivery.py`

- [ ] Add a two-record test asserting one `send_group_forward_msg` call and ordered text/image node content.
- [ ] Add a failure test asserting exactly one combined plain-text fallback and no per-record sends.
- [ ] Keep the existing single-record mixed-message assertion.
- [ ] Run `TARGET_GROUP_ID=135792468 .venv/bin/python -m unittest tests.test_reply_delivery -v` and confirm the new tests fail on the loop-based sender.

### Task 2: Implement merged-forward delivery

**Files:**
- Modify: `plugins/violation_record/matcher.py`

- [ ] Extract a helper that builds a `Message` from one record and filters missing local images.
- [ ] For two or more records, build `MessageSegment.node_custom` nodes and call `send_group_forward_msg` once.
- [ ] On API failure, log one warning and send one combined text fallback with an image notice when applicable.
- [ ] Run focused and full tests.

### Task 3: Record and deploy v1.0.2.5beta

**Files:**
- Modify: `CHANGELOG.md`
- Create: `docs/releases/2026-08-18-v1.0.2.5beta.md`

- [ ] Record behavior, compatibility boundary and rollback instructions.
- [ ] Back up only the files replaced on the server.
- [ ] Restart only `qq-violation-bot.service` and verify the OneBot WebSocket reconnects.
