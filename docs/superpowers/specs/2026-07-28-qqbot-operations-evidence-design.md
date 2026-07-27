# QQ Bot Operations, Logging, and Evidence Design

Date: 2026-07-28

Status: Approved for implementation planning

Target: `/opt/qq-violation-bot` on the production server

## 1. Purpose

This design adds operational containment for the NapCat resource leak, strict
chat isolation for one target group, persistent evidence images linked to
violation records, safer SQLite backups, a disabled-by-default mute switch, and
Git/GitHub maintenance.

The existing violation-record business behavior is the protected baseline. The
implementation must preserve NLP intent routing, validation, member resolution,
record selection, record ordering, state calculation, confirmation, withdrawal,
and automatic deduction behavior unless this document explicitly defines an
exception.

## 2. Non-Negotiable Boundaries

1. Only the group identified by the runtime `TARGET_GROUP_ID` is in scope for
   chat processing and persistence. The production value is not stored in Git.
2. Messages from every other group may reach the OneBot/NoneBot network ingress,
   but must then be discarded. They must not trigger NLP, business functions,
   database writes, member synchronization, media downloads, chat archives, or
   custom message logs.
3. The only approved user-facing business exceptions are:
   - an optional evidence reminder/requirement for new violation records;
   - evidence images displayed with query results;
   - a configuration switch that disables mute execution by default.
4. Existing query SQL, filtering, ordering, count calculation, and state
   calculation remain unchanged. Only the result transport and rendering layer
   may be extended for evidence.
5. Existing records without evidence are valid. Missing evidence must never
   cause a query error.
6. Root execution, broad model access, and current database file permissions are
   unchanged.
7. Query index work is deferred to a separate discussion.

## 3. Delivery Modules

The work is divided into independently testable and reversible modules:

1. repository and public-source hygiene;
2. target-group boundary and chat archive;
3. evidence capture, storage, binding, and query rendering;
4. mute switch;
5. NapCat resource watchdog, cleanup, and scheduled restart;
6. SQLite online backup;
7. deployment, verification, rollback, and GitHub publication.

Each module receives a separate commit. A failure in one module must not require
reverting unrelated modules.

## 4. Target-Group Boundary and Logging

### 4.1 Ingress behavior

The current NapCat build does not expose a usable server-side group event filter.
NapCat will therefore continue delivering OneBot events to NoneBot. The first
application-level rule must reject any group whose ID does not equal the runtime
`TARGET_GROUP_ID`.

Rejected events have no application side effects. In particular, the handler
must return before admin registration, group-member synchronization, intent
parsing, service calls, or file access.

### 4.2 Framework and operational logs

NoneBot console logging changes to `WARNING` so normal `SUCCESS` and `INFO` event
lines do not persist group IDs, QQ IDs, text, image metadata, or image URLs in the
systemd journal.

A logging filter suppresses default message-event records. Application exception
logs contain only the processing stage, exception class, target group ID, and
`message_id`; they do not interpolate message text or media URLs. NapCat file
logging stays disabled. Connection, crash, restart, backup, and resource metrics
remain available as operational logs because they do not contain chat bodies.

### 4.3 Target-group chat archive

A passive archive handler runs only for the target group and records messages in
`data/chat_archive.db`. It does not block or alter the existing violation matcher.

The archive stores:

- `message_id` as the idempotency key;
- target `group_id`;
- event timestamp;
- sender QQ ID and sender metadata JSON;
- complete OneBot message-segment JSON;
- plaintext representation;
- reply/referenced message ID when present;
- archive creation timestamp.

Ordinary image segments are archived as metadata only. Their image bytes are not
downloaded. Only images referenced while creating a violation record enter the
evidence-download path.

The archive retains target-group records indefinitely. It never stores an event
from another group.

## 5. Evidence Architecture

### 5.1 Scope and configuration

Evidence capture applies only when all of these conditions hold:

1. the event belongs to the target group;
2. the event is addressed to the bot;
3. the parsed intent is `create_violation`;
4. the command replies to or references a message containing one or more image
   segments.

`EVIDENCE_REQUIRED=false` is the default. In this mode, a record without a
referenced image receives a reminder but may proceed through the existing preview
and confirmation flow. When explicitly set to `true`, a missing referenced image
blocks creation before a pending operation is created.

No NLP prompt or schema change is required. Evidence metadata is attached after
intent parsing by deterministic application code.

### 5.2 Storage layout

Evidence metadata uses a separate SQLite database at `data/evidence.db`. Binary
files use a content-addressed tree under `evidence/images/`, for example:

```text
evidence/images/ab/abcdef...1234.jpg
```

The file name is derived from SHA-256, allowing deterministic deduplication. The
database contains these logical tables:

- `evidence_files`: hash, relative path, MIME type, byte size, source group,
  source message, creation time, and file status;
- `evidence_batches`: group, operator QQ, command message, lifecycle state,
  creation time, and expiry time;
- `evidence_batch_items`: ordered mapping from a batch to one or more files;
- `violation_evidence`: violation ID, target QQ, evidence file ID, ordinal, and
  binding time.

The evidence database intentionally has no foreign key into the business
database. `violation_id` and target QQ provide an external association while
keeping the business schema unchanged.

### 5.3 Download safety

Downloads are streamed with a bounded timeout and a maximum size of 20 MiB per
image. The downloader accepts only HTTP(S) image sources obtained from OneBot
message segments, rejects loopback/private destinations after URL validation,
checks image MIME type and file signature, and writes to a `.part` file before an
atomic rename.

Evidence directories use root-only permissions. Logs never contain full image
URLs. Duplicate image content reuses the existing hash-addressed file.

### 5.4 Preview, confirmation, and cancellation

At preview time, referenced images are downloaded into an evidence batch. The
existing pending-operation payload receives only an `evidence_batch_id`; all
business record fields remain unchanged.

On confirmation:

1. the violation record is inserted using the existing transaction and business
   rules;
2. the committed `violation_id` is returned internally;
3. the evidence batch is bound to that ID and the target QQ in the separate
   evidence database.

Evidence binding is deliberately downstream of the business commit. An evidence
failure cannot roll back a valid violation record. Failed binding leaves a
recoverable batch for a background retry and writes a sanitized operational
warning.

Cancellation or expiry marks the batch unbound. Unbound cancelled/expired batches
older than seven days are eligible for cleanup. Bound evidence is never removed
automatically, including when a violation is later soft-withdrawn.

## 6. Evidence-Aware Query Rendering

The approved approach is a structured display result. Query selection remains in
the current service layer. The existing rows, IDs, ordering, summary values, and
record text are passed to a narrow renderer that adds evidence attachments.

For a query with records:

1. the first OneBot message contains the existing member/area summary and the
   existing text for record 1, followed by every image associated with record 1;
2. every later record is sent as its own OneBot message containing the existing
   record text followed by all of its images;
3. one record is one OneBot send operation built from mixed text and image
   segments, giving QQ the best chance to render the text and images in one chat
   bubble;
4. there is no evidence-image cap;
5. a record with zero evidence sends text only;
6. a missing, corrupt, or unsendable image is skipped without aborting the record
   text or later records.

If the query has no records, its current response remains unchanged. The QQ client
ultimately controls rendering, so one mixed OneBot send is the enforceable
boundary; client-side splitting cannot be completely prevented.

Regression fixtures must compare the pre-change and post-change summary values,
record text, record order, current count, and state exactly.

## 7. Mute Switch

`MUTE_ENABLED=false` disables execution by default. NLP may continue recognizing
`mute_member`; the moderation entry point checks the switch before any OneBot API
call and returns a short disabled response. No mute request reaches
`set_group_ban` while the switch is false.

No other model permissions are changed.

## 8. Resource Watchdog and Scheduled Restart

The resource leak is in the NapCat/QQ/Node/Xvfb process layer, not the Python bot
or SQLite connection layer. The mitigation therefore restarts only
`napcat.service`.

### 8.1 Five-minute watchdog

A systemd timer runs every five minutes. A restart is triggered when any resource
condition is met, or when the WebSocket condition is met on two consecutive
checks:

- QQ/Node total file descriptors: at least 1500;
- repeated `/proc/<pid>/maps` descriptors: at least 1000;
- Xvfb total file descriptors: at least 220;
- recent `Maximum number of clients reached` log event;
- reverse OneBot WebSocket absent for two consecutive checks.

A 30-minute cooldown state prevents restart loops. The watchdog records only
process IDs, counts, decisions, timestamps, and post-check status.

### 8.2 Fixed restart

A second timer restarts NapCat daily at 04:10, after the current 03:30 database
backup window. It does not restart `qq-violation-bot.service`.

After either restart, the checker waits up to 90 seconds for:

- new NapCat/QQ processes;
- file-descriptor counts below thresholds;
- an established reverse WebSocket to `127.0.0.1:6199`;
- the Python bot service to remain active.

A failed post-check produces one warning and exits. It does not loop blindly.

The currently observed process state already exceeds the proposed threshold, so
the first enabled health check is expected to cause one controlled NapCat restart
and a temporary bot connection interruption of approximately 30-90 seconds.

### 8.3 Safe cleanup

Automatic cleanup is limited to reproducible temporary state:

- incomplete `.part` downloads older than one hour;
- unbound cancelled/expired evidence batches older than seven days;
- stale watchdog lock and state files.

Bound evidence, target-group chat archives, violation data, exports, and formal
backups are not deleted. Backup retention remains out of scope until separately
approved.

The existing orphan Xvfb process is terminated only after a fresh read-only check
confirms that its PID, display, and clients do not belong to the active NapCat
service.

## 9. SQLite Backup

The current live-file copy is replaced by the Python SQLite Online Backup API.
The backup writes to a temporary destination, runs `PRAGMA integrity_check`, and
is atomically renamed only after validation. Backup failure leaves the source
database untouched and produces a sanitized operational warning.

The existing schedule remains in place. No existing backup is automatically
deleted.

## 10. Public GitHub Repository

The repository will be published as:

```text
ColdSpellhere/qq-violation-bot
```

Visibility is public. Runtime data and secrets must never enter Git history.

The pre-publication scan has already identified real runtime identifiers in
source-adjacent files. Before the source baseline is committed:

- `config.py` must require the target group from runtime configuration instead of
  containing a production fallback;
- the NapCat start script must obtain the bot QQ ID from a protected runtime
  environment rather than a literal argument;
- README examples and `.env.example` must use synthetic placeholders;
- the real `.env` remains unchanged and ignored.

The repository ignores `.env`, virtual environments, databases, evidence,
archives, backups, exports, logs, import reports, NapCat runtime files, caches,
and partial downloads. The public baseline is not committed until staged-file and
secret scans pass.

The server currently has Git but no GitHub CLI authentication. Publication uses a
separate authenticated step without placing a password or token in the project,
shell history, or chat transcript.

## 11. Testing Strategy

Implementation follows test-first development. Tests use temporary directories
and temporary SQLite databases; they never insert synthetic violations into the
production database.

Required regression coverage includes:

- existing member and area query selection, order, text, count, and state;
- old records with no evidence;
- one record with multiple evidence images;
- failed download, corrupt image, missing file, and failed send isolation;
- soft and hard `EVIDENCE_REQUIRED` modes;
- confirmation success with later evidence-binding failure;
- group-outside-target no-side-effect behavior;
- complete target-group chat archive and image-metadata-only behavior;
- mute disabled before any OneBot API call;
- watchdog thresholds, consecutive WebSocket checks, cooldown, and stale-state
  cleanup;
- SQLite online backup integrity and failure behavior.

Static and service validation includes Python compilation, shell syntax checks,
dependency consistency, systemd unit verification, SQLite integrity checks, and a
review of the final Git diff.

## 12. Deployment and Rollback

Before deployment, create separate backups of source, `.env`, systemd units, and
the production SQLite databases. Preserve file ownership and modes.

Deployment order:

1. public-source hygiene, staged secret scan, sanitized source baseline, and test
   harness;
2. target-group boundary, logging, and archive;
3. evidence storage and capture;
4. evidence-aware query rendering and mute switch;
5. online backup;
6. watchdog, timers, and controlled NapCat restart;
7. final secret/history scan and public GitHub push.

Each stage is validated before the next begins. A stage that fails validation is
rolled back without deleting newly collected chat or evidence data.

Rollback controls are independent:

- restore source and `.env` from the pre-deployment snapshot;
- disable archive/evidence loading to restore the original reply path;
- disable watchdog and restart timers without stopping the bot;
- restore prior systemd unit files and run `systemctl daemon-reload`;
- retain the business database and all bound evidence.

The SSH ControlMaster connection remains open throughout the work. No command
intentionally exits the server session.

## 13. Acceptance Criteria

The work is accepted when all of the following are verified:

1. `qq-violation-bot.service` and `napcat.service` are active and the reverse
   OneBot WebSocket is established.
2. NapCat/QQ/Xvfb descriptor counts return below watchdog thresholds after the
   controlled restart.
3. Existing target-group record creation, confirmation, query, status, withdrawal,
   and automatic calculations retain their previous results.
4. A new violation can bind multiple referenced images.
5. Query output pairs each record with all of its evidence in one mixed OneBot send.
6. Historical records without evidence return normally.
7. Events outside the target group create no NLP calls, application writes,
   archives, downloads, or chat-content logs.
8. Bound evidence survives restart and soft withdrawal.
9. Backups pass SQLite integrity checks.
10. The public Git history contains no runtime secrets, real chat data, evidence,
    databases, or production identifiers.

## 14. Explicitly Deferred Work

- query index changes;
- root-service privilege reduction;
- database permission changes;
- model permission reduction;
- backup retention deletion;
- unrelated NLP, query, and business refactoring.
