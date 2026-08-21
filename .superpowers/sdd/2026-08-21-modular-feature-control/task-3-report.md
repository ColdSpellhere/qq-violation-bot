# Task 3 Report: Independent Group Router and Chat Gates

## Status

Implemented and verified. The implementation commit is `0979b77` (`feat: route business and chat independently`). Scheduler and Task 4 files were not modified.

## Files

Created:

- `plugins/group_router/__init__.py`
- `plugins/group_router/matcher.py`
- `tests/test_group_router.py`

Modified:

- `bot.py`
- `plugins/violation_record/matcher.py`
- `plugins/random_chat/matcher.py`
- `plugins/chat_archive/matcher.py`
- `plugins/member_memory/matcher.py`
- `plugins/private_chat/matcher.py`
- `tests/test_violation_chat_fallback.py`
- `tests/test_random_chat_context.py`
- `tests/test_chat_archive.py`
- `tests/test_member_memory.py`
- `tests/test_private_chat.py`
- `tests/test_plugin_loading.py`

## Implementation

- Converted the violation-record group matcher into `handle_business_message(bot, event, text) -> bool` without importing the chat module. Fixed policy commands, structured replies, export upload, evidence capture, admin synchronization, mute handling, intent delivery, and existing error messages remain in the business module. An `unknown` parsed intent now returns `False` without sending so the router can decide whether chat is allowed.
- Added the sole group response matcher in `plugins.group_router.matcher` at priority 8 with `block=True`. Its candidate accepts the configured business group or a group allowed by `FEATURES.group_chat_allowed`, and always rejects messages sent by the bot itself.
- The router extracts mention-free text once. Addressed business messages first enter the business handler; recognized requests stop. Unknown requests may fall through to the shared group-chat gate. Addressed allowed messages bypass probability sampling, while ordinary allowed text uses the existing eligibility and probability policies.
- Removed the standalone random-chat matcher registration while retaining `send_random_reply` and its context, profile, AI, sticker, and delivery behavior.
- Bound archive priority 1 to `FEATURES.group_chat_allowed` and passed the event's actual group ID to `archive_payload` and identity storage.
- Bound member-memory priority 2 to the same group gate. The gate is checked during candidate selection, again before enqueue, at delayed callback entry, and immediately before storage writes.
- Bound private chat to `FEATURES.private_chat_allowed(user_id)` plus the existing self-message exclusion. This requires the global chat parent switch, the private-chat child switch, and the private allowlist through the approved controller interface.
- Kept Task 2 plugin loading explicit. `plugins.feature_control` and its matcher now load immediately after the business plugin and before any consumer imports `feature_control.runtime`; this avoids NoneBot treating the package as an ordinary pre-imported module.

## RED Evidence

Tests were written before production changes. Command:

```bash
.venv/bin/python -m unittest tests.test_group_router tests.test_chat_archive tests.test_member_memory tests.test_private_chat -v
```

Observed exit code 1 with 11 errors. The first failure was `ModuleNotFoundError: No module named 'plugins.group_router'`. Archive, memory, and private tests separately failed because those modules did not yet expose/use `FEATURES`. This matched the expected missing router and shared-gate behavior.

## GREEN and Verification Evidence

- The same RED command passed after implementation: 37 tests, 0 failures.
- The complete affected command from the brief passed: 60 tests, 0 failures.
- A plugin-loading regression initially exposed a real integration issue: `group_router` imported `feature_control.runtime` before NoneBot registered the parent plugin. Reordering only the explicit plugin loads fixed the root cause; `tests.test_plugin_loading` then passed.
- The broader repository suite was run with the repository-required synthetic runtime group and the public-secret boundary test excluded: 271 tests, 0 failures. The public-source boundary was then run separately with a synthetic ID absent from tracked files: 4 tests, 0 failures.
- `python -m compileall -q bot.py plugins scripts tests` exited 0.
- `git diff --check` exited 0.
- `scripts/check_public_tree.py` reported `public source scan: PASS` after staging the new files.
- The live NoneBot matcher registry after importing `bot` was:

  - priority 0: `plugins.feature_control.matcher`, blocking
  - priority 1: `plugins.chat_archive.matcher`, non-blocking
  - priority 2: `plugins.member_memory.matcher`, non-blocking
  - priority 5: `plugins.private_chat.matcher`, blocking
  - priority 8: `plugins.group_router.matcher`, blocking

  There are no registered response matchers from `plugins.violation_record.matcher` or `plugins.random_chat.matcher`.

## Compatibility Decisions

- Business internals were kept in their original module; only matcher registration and matcher-specific `finish()` control flow were replaced with direct group delivery and boolean completion. This keeps business behavior independent of chat and lets one router own response arbitration.
- `RANDOM_CHAT_DIRECT_FALLBACK_ENABLED` remains accepted in configuration for deployment compatibility, but unknown addressed routing is now governed by the shared runtime chat parent/child/allowlist gates, as required by the approved design.
- The existing random-chat policy helpers remain available even though candidate registration moved to the router.
- Archive and member-memory now support every runtime-allowed chat group rather than only `CONFIG.target_group_id`; all storage calls are scoped by the actual event group to prevent cross-group context mixing.
- Task 2's feature-control parent plugin and command matcher remain explicitly loaded. Only their position in the load sequence changed to satisfy NoneBot plugin ownership rules.

## Concerns

- Some existing member-memory tests deliberately exercise database/mirror failures and emit warning tracebacks; they pass and are unrelated to Task 3.
- Full discovery requires a numeric `TARGET_GROUP_ID`. The public-source boundary must use a different synthetic value that does not occur in tracked fixtures, per the repository's existing verification plan. Running discovery with no ID, or running the leak scanner with the common fixture ID, produces environment/test-harness failures rather than product failures.
- No scheduler behavior was exercised beyond the full repository regression suite because Task 4 and scheduler changes were explicitly out of scope.
