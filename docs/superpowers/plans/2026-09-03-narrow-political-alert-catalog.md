# Political Leader Direct Alert Catalog v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the legacy broad political rules with a private, reviewable catalog containing distinctive historical-event names and the broadest defensible post-1949 roster returned by provincial-ministerial or military-equivalent rank filters and above across the source's party, state, military, and mass-organization scope. Historical events and complete leader names both match directly without an additional context gate. Keep alerts visible only to the management group and leave Kona unchanged. The roster remains a research candidate snapshot rather than an official or provably exhaustive historical register.

**Architecture:** Keep the existing immutable, instance-private catalog generations and literal trie. The current version-2 build contract uses only direct `historical_event` and `leader_name` entries; the runtime temporarily retains read compatibility for the previous contextual v2 generation so pointer-first rollback remains possible. A complete catalog term in a OneBot text segment emits a match immediately. The source roster is private data, not repository content. Third-party research data may seed candidates, but provenance and confidence remain explicit and official records are retained as the verification target. Preserve atomic pointer switching, last-known-good behavior, backup, and rollback.

**Tech Stack:** Python 3.11, NoneBot2/OneBot V11, immutable JSON generations, `unittest`, systemd deployment scripts.

---

### Task 1: Preserve the safe v1 transition baseline

**Files:** `scripts/import_content_alert_catalog.py`, `plugins/content_alert/catalog.py`, `plugins/content_alert/service.py`, focused tests, `README.md`.

- [ ] Keep new political generations management-visible while still reading the previous hidden v1 generation during transition.
- [ ] Require reviewed political subjects, canonical terms, pinned sources, review dates, and no aliases.
- [ ] Make every sensitive subject in an enabled v2 category directly alerting; reject `shadow`/`disabled` entries there and reject legacy shadow merging into v2 before any write. Read old `support_only` entries only for rollback compatibility.
- [ ] Prove hidden non-political generations still redact, and visible political alerts show the sender and bounded excerpt only in the management group.

### Task 2: Define and enforce the v2 import contract with TDD

**Files:** `scripts/import_content_alert_catalog.py`, `tests/test_import_content_alert_catalog.py`.

- [ ] Add v2 document, pointer, manifest, and shard support while preserving v1 rollback validation.
- [ ] Model new production subjects as `leader_name` or `historical_event`, both with `match_mode=direct`; retain contextual types only in the runtime compatibility reader.
- [ ] Require leader `entity_ref`, rank level, rank basis, source references, review date, and confidence/verification metadata.
- [ ] Preserve bounded validation for old support-context generations without generating such entries in the new production build.
- [ ] Reject inconsistent subject/mode combinations, aliases, missing provenance, invalid ranks, duplicate entities, oversized data, and unsupported versions.

### Task 3: Implement direct complete-name matching with TDD

**Files:** `plugins/content_alert/engine.py`, `plugins/content_alert/catalog.py`, `tests/test_content_alert_catalog.py`.

- [ ] Preserve every literal occurrence while keeping the existing deduplicated API compatible.
- [ ] Emit historical-event and complete leader-name matches directly.
- [ ] Match only a normalized complete catalog string inside one text segment; never infer aliases, name fragments, homophones, or text from images/replies/non-text segments.
- [ ] Check repeated occurrences and report each canonical name only once per message.
- [ ] Prove a complete leader name needs no context while legacy contextual generations and v1 rollback remain readable.

### Task 4: Render direct matches safely

**Files:** `plugins/content_alert/service.py`, `tests/test_hive_keyword_alert.py`.

- [ ] Report the matched complete leader name without exposing internal IDs, catalog paths, or the roster.
- [ ] Center a bounded excerpt around the direct match using a normalized-to-original offset map.
- [ ] Preserve one alert per source message, sender identity, source-group isolation, management-group-only delivery, peer-bot suppression, and the runtime feature switch.
- [ ] Keep the detector independent of AI and all violation/business decisions.

### Task 5: Build the private post-1949 roster and event catalog

**Files:** private work area only; never Git. Final CArroT path: `/opt/qq-bots/instances/carrot/data/content_alert/private-builds/`.

- [ ] Collect every publicly searchable national principal/deputy, provincial-ministerial principal/deputy, deputy/full theater-command, and military-commission-member result from the chosen research index, retaining source record IDs, rank/page evidence, retrieval time, page content digests, and a complete snapshot ID.
- [ ] Normalize traditional/simplified canonical names conservatively; activate one matcher subject per canonical full-name string, group indistinguishable same-name identities under that subject, and retain every underlying identity plus variant source text in an instance-private audit sidecar.
- [ ] Seed official-verification layers from State Council, NPC, CPPCC, Party history, courts/procuratorates, provincial gazettes, and organization-history sources; never claim records are official when only a research source exists.
- [ ] Preserve the source's broad party/state/military/mass-organization rank-filter semantics. Do not infer a narrower civilian-only scope from a current display position, and do not claim that a research candidate is individually official-verified.
- [ ] Merge the reviewed historical-event set; do not copy the obsolete support-context set into the new direct generation.
- [ ] Validate counts, source coverage, duplicate IDs/names, character normalization, empty/oversized fields, aliases, provenance, file permissions, and private/public separation.

### Task 6: Perform adversarial and rollback validation

- [ ] Run focused red/green tests and the complete suite with the same synthetic environment used by deployment.
- [ ] Exercise every complete name and event as a standalone direct hit, plus repeated names, full-width/zero-width normalization, segment separation, overlapping terms, tampered shards, stale pointers, cold start, LKG retention, and v1/v2 rollback.
- [ ] Import into a disposable instance, switch generations atomically, verify counts without printing terms, then roll back and verify the original pointer and behavior.
- [ ] Run compile checks, public-tree current/history scans, secret/privacy scans, and a targeted search proving no private roster term or data artifact entered Git.

### Task 7: Deploy and verify CArroT only

- [ ] Capture CArroT and Kona release pointers, service state, configuration hashes, and managed-catalog pointers before deployment.
- [ ] Create a clean candidate commit and push only to the server candidate ref; deploy only `qqbot@carrot` and CArroT's private catalog with backups.
- [ ] Leave `qqbot@kona`, `napcat@kona`, Kona configuration, data, catalog, and release pointer untouched; compare all captured Kona values after deployment.
- [ ] Verify CArroT service, NapCat service, reverse WebSocket, OneBot identity, target-group membership, feature state, catalog LKG, and representative synthetic matching without sending sensitive test content to QQ.
- [ ] Verify rollback restores both code and catalog pointer, then restore the accepted candidate and repeat health checks.
- [ ] Inspect logs for new tracebacks/errors and observe stable operation before completion.

### Task 8: Final repository and requirement audit

- [ ] Review every requirement against authoritative evidence, inspect the complete diff, and remove unrelated changes.
- [ ] Re-run the complete test and scan gates after the final edit.
- [ ] Push the accepted source history according to the established repository workflow only after CArroT validation; do not promote or redeploy Kona.
- [ ] Report actual coverage and source confidence honestly; list any intrinsically unverifiable historical-data gaps rather than claiming false completeness.
