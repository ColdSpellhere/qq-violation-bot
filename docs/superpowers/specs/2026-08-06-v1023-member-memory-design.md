# v1.0.2.3beta Conservative Member Memory Design

## Goal

Improve random group-chat replies by distinguishing who is speaking to whom and remembering a small set of stable, explicitly stated member traits. Keep random triggering at 5% and isolate the feature from moderation and violation-record workflows.

## Conversation identity

Every context item carries message ID, QQ user ID, display name, text, mentioned QQ IDs, reply message ID, and replied-to QQ ID. The prompt renders these relationships explicitly. A message addressed to another member must not be interpreted as addressed to the bot; ambiguous turns should produce `SKIP`.

## Conservative memory

Memory is keyed by group ID and QQ user ID. Deterministic identity updates preserve the latest display name and bounded aliases. AI extraction may add only stable facts explicitly stated by that same member. It must reject third-party claims, jokes, temporary moods, sensitive data, and unsupported inference. Each trait stores evidence message ID and update time; at most eight traits are retained per member.

The SQLite table is authoritative. A JSON mirror at `data/member_memory/<group_id>/<user_id>.json` is written atomically for human inspection. Runtime JSON files remain ignored by Git; the repository tracks only the directory policy and documentation.

## Runtime flow

The archive matcher updates identity after successfully archiving a target-group message. When a 5% random-chat trigger fires, the bot loads structured recent context and relevant profiles, generates a reply, sends it if non-silent, then performs one best-effort memory extraction over the same bounded context. Memory extraction failure only logs a warning and never suppresses or delays an already generated reply.

## Release and rollback

The release is `v1.0.2.3beta`. Before deployment, back up `chat_archive.db`. Rollback disables random chat or returns to `v1.0.2.2beta`; the additive memory table and ignored JSON files may remain without affecting older code.
