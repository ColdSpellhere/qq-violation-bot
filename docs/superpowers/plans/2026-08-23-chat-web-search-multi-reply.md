# Chat Web Search and Multi-Reply Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add chat-only Tavily web search and bounded model-directed multi-message replies without changing business decisions or cross-instance isolation.

**Architecture:** A dedicated web-search package owns policy, typed results, HTTP transport, and lazy lifecycle. Chat prompt inputs carry bounded untrusted search data, while a strict reply parser converts model JSON into one to three messages and the existing matchers deliver them sequentially with per-send persistence.

**Tech Stack:** Python 3.10+, NoneBot2, OneBot V11, httpx, unittest, existing LLM Gateway and Prompt Builder.

---

### Task 1: Runtime configuration and feature switch

**Files:**
- Modify: `.env.example`
- Modify: `plugins/violation_record/config.py`
- Modify: `plugins/feature_control/state.py`
- Modify: `plugins/feature_control/runtime.py`
- Modify: `plugins/feature_control/commands.py`
- Test: `tests/test_feature_control.py`
- Test: `tests/test_feature_control_commands.py`

- [ ] Add failing tests asserting `web_search_enabled` defaults to false for legacy JSON, rejects non-boolean persisted values, is instance-persisted, appears in `/模块状态`, and can only be changed by the existing superuser matcher.
- [ ] Run `TARGET_GROUP_ID=817263540 .venv/bin/python -m unittest tests.test_feature_control tests.test_feature_control_commands -v`; expect failures for the missing field and command.
- [ ] Add bounded Tavily configuration and `/联网搜索 开|关` using the existing controller persistence path; never include the Key in state or status output.
- [ ] Re-run the focused tests; expect all tests to pass.

### Task 2: Search policy, client, and lifecycle

**Files:**
- Create: `plugins/web_search/__init__.py`
- Create: `plugins/web_search/models.py`
- Create: `plugins/web_search/policy.py`
- Create: `plugins/web_search/client.py`
- Create: `plugins/web_search/runtime.py`
- Modify: `bot.py`
- Create: `tests/test_web_search_policy.py`
- Create: `tests/test_web_search_client.py`
- Create: `tests/test_web_search_lifecycle.py`
- Modify: `tests/test_plugin_loading.py`

- [ ] Add failing tests for explicit/time-sensitive triggers, non-triggering casual text, 200-character query cap, and absence of QQ/history/profile data.
- [ ] Add failing client tests for fixed endpoint, Bearer auth, bounded result parsing, timeout/auth/rate/server/contract errors, one retry only for retryable classes, cancellation propagation, and idempotent close.
- [ ] Add failing lifecycle tests proving disabled startup creates no client, concurrent first access is single-flight, shutdown closes once, and package import has no NoneBot registration side effect.
- [ ] Run the three new test modules plus plugin loading; expect import failures for the missing package.
- [ ] Implement immutable models, conservative policy, injected `httpx.AsyncClient`, typed redacted errors, lazy runtime, and explicit lifecycle loading in `bot.py`.
- [ ] Re-run focused tests; expect all tests to pass without network access.

### Task 3: Prompt search-data boundary

**Files:**
- Modify: `plugins/chat_prompt/models.py`
- Modify: `plugins/chat_prompt/budget.py`
- Modify: `plugins/chat_prompt/builder.py`
- Modify: `plugins/random_chat/ai.py`
- Test: `tests/test_chat_prompt_builder.py`
- Test: `tests/test_chat_prompt_budget.py`
- Create: `tests/test_chat_web_search_prompt.py`

- [ ] Add failing tests that search results appear only in the user-role `<web_search_data>` section, are escaped, respect a 4000-character budget, and cannot enter business prompts or system/persona rules.
- [ ] Add failing tests for search-failed status preventing a false “already searched” claim in both Builder and legacy paths.
- [ ] Run the prompt test set; expect failures for missing typed fields and rendering.
- [ ] Extend `ChatPromptInput` and budgeted data with typed search context and failure state; render only bounded data and retain fixed security precedence.
- [ ] Add chat-only search orchestration that rechecks the runtime switch before model invocation and silently degrades on typed search errors.
- [ ] Re-run prompt and adjacent random/private chat tests; expect all tests to pass.

### Task 4: Strict one-to-three reply contract

**Files:**
- Modify: `plugins/llm_gateway/gateway.py`
- Modify: `plugins/random_chat/ai.py`
- Create: `tests/test_chat_multi_reply.py`
- Modify: `tests/test_llm_gateway_chat_migration.py`

- [ ] Add failing tests for JSON `messages`, maximum three for private/addressed, maximum one for random group chat, empty/repeated/overlong item rejection, `SKIP`, and plain-text single-message fallback.
- [ ] Add failing migration tests proving the Gateway-off single-message legacy behavior remains accepted and image replies do not disappear when JSON mode is unavailable.
- [ ] Run the new and migration tests; expect failures for missing `generate_replies()` and reply parser.
- [ ] Implement a strict bounded parser and `generate_replies()` while retaining `generate_reply()` as the compatibility wrapper.
- [ ] Re-run the focused tests; expect all tests to pass.

### Task 5: Sequential delivery and persistence

**Files:**
- Modify: `plugins/random_chat/matcher.py`
- Modify: `plugins/private_chat/matcher.py`
- Modify: `plugins/private_chat/conversation.py` if required for unique assistant message identifiers
- Modify: `tests/test_random_chat_context.py`
- Modify: `tests/test_private_memory_integration.py`
- Create: `tests/test_chat_multi_reply_delivery.py`

- [ ] Add failing tests for private/addressed three-message delivery, random-group one-message limit, 350 ms injected interval, final-message-only sticker, stop-on-failure, and “persist only successfully sent private messages” with unique identifiers.
- [ ] Run delivery and adjacent persistence tests; expect failures because matchers send one string.
- [ ] Implement the shared sequential delivery helper and adapt both matchers without changing business matchers.
- [ ] Re-run focused delivery, random chat, private chat, memory replay, and plugin loading tests; expect all tests to pass.

### Task 6: Documentation, secret scan, and deployment

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `scripts/check_public_tree.py` only if a new fixture requires a narrow synthetic-token construction; do not weaken current-tree scanning.
- Test: `tests/test_public_source.py`
- Test: `tests/test_public_scanner.py`

- [ ] Document `/联网搜索`, privacy boundaries, Tavily quota, multi-message limits, failure behavior, instance-specific configuration, smoke checks, and rollback without including a real Key.
- [ ] Run focused tests, full `unittest discover`, `compileall`, `git diff --check`, current-tree and history public scans; every command must exit zero.
- [ ] Back up both instance `.env` files and current release links. Update each `.env` with the Key using a non-logging structured updater and mode `0600`.
- [ ] Create a new immutable release from the verified working tree, deploy CArroT first, run a real Tavily search smoke test and instance health check, then deploy kona and repeat health checks.
- [ ] Verify both qqbot and NapCat units are active, reverse WebSockets are connected, CArroT business remains enabled, kona remains `chat_only`, and data/character/sticker paths remain instance-isolated.
- [ ] If any production check fails, disable `web_search_enabled`, restore the previous release link and `.env`, restart only the affected qqbot unit, and re-run health checks.
