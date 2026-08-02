# v1.0.2beta Deduction Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变现有 NLP、查询文本、记录预览确认、证据图片和群交互契约的前提下，上线可审计、可回放、可人工决策的 v1.0.2beta 减数策略引擎，并按批准表同步 127 个成员群域的当前次数基线。

**Architecture:** 新策略完全使用 `v102_*` SQLite 命名空间，与 2026-07-16 遗留策略表隔离。现有业务记录先按原事务提交，随后通过 `source_record_id` 进入独立、幂等的策略事务；规则结算不依赖 NapCat，通知经 outbox 在连接恢复后补发。首次基线同步只由专用迁移 CLI 执行，服务启动仅校验迁移检查点。

**Tech Stack:** Python 3.10、NoneBot2、OneBot V11、SQLite、标准库 `unittest`、`openpyxl`、systemd。

---

## Fixed Boundaries

- 不修改 `ai_router.py`、`schemas.py`、`member_resolver.py` 的行为。
- 不修改现有查询结果文本、排序和证据图片混合消息。
- 不修改违规记录的输入、预览、确认、取消和成功回复文本。
- 不复用 `member_policy_state`、`state_transitions`、`schema_migrations`。
- 生产配置值、准确 XLSX、成员明细、数据库备份和迁移报告不得进入 Git。
- `DEDUCTION_POLICY_V102_ENABLED=false` 时只运行旧引擎；为 `true` 时只运行 v102 引擎。

## File Map

- Create `plugins/violation_record/policy_schema.py`: v102 DDL、版本检查、索引和显式连接辅助。
- Create `plugins/violation_record/deduction_policy.py`: 严重度解析、事件归约、周期、结算、回放和 outbox。
- Create `plugins/violation_record/policy_commands.py`: 固定命令解析、预览、查询和人工决定。
- Create `scripts/migrate_v102.py`: XLSX 预演、迁移、验证和逻辑回滚。
- Modify `plugins/violation_record/config.py`: 功能开关和规则版本。
- Modify `plugins/violation_record/db.py`: 幂等 schema 检查和统一基线偏移计数适配。
- Modify `plugins/violation_record/service.py`: 原业务提交后的策略联动及人工命令确认。
- Modify `plugins/violation_record/matcher.py`: 目标群过滤后、NLP 前固定命令入口。
- Modify `plugins/violation_record/scheduler.py`: 离线结算、outbox 发送、唯一任务句柄和 shutdown。
- Modify `plugins/violation_record/exporter.py`: 周报附加策略操作日志，不改变原表。
- Modify `plugins/violation_record/formatter.py`: 仅增加固定命令帮助。
- Create `tests/test_policy_schema.py`、`tests/test_deduction_policy.py`、`tests/test_policy_commands.py`、`tests/test_policy_integration.py`、`tests/test_policy_scheduler.py`、`tests/test_v102_migration.py`。
- Modify `CHANGELOG.md`、`README.md`、`.env.example`: v1.0.2beta 文档和开关说明。

## PRD Coverage Matrix

| PRD requirement | Implementation task |
|---|---|
| 目标群隔离、群外零副作用 | Task 7、Task 11 |
| 普通 14 天周期与减数 | Task 3 |
| 递增减缓、第二条延长、第三条建议 | Task 3 |
| 普通减停、固定 30 天端点、人工决定 | Task 4、Task 6 |
| 最后警告 90 天、恢复和移出提醒 | Task 4 |
| 撤回完整回放和人工因果 | Task 5 |
| 补录最近周期重算和久远记录边界 | Task 5 |
| 人工封闭节点 | Task 5、Task 6 |
| 固定管理命令、二次确认和查询名单 | Task 6 |
| 新增违规自动联动、崩溃补偿 | Task 7 |
| NapCat 离线结算、通知 outbox、每小时提醒 | Task 8 |
| 全部自动和人工日志、周报 | Task 9 |
| 准确表基线、排除低频/封存/黑名单/OOPZ | Task 10 |
| 回滚、生产切换与公开 GitHub 敏感扫描 | Task 11、Task 12 |

### Task 1: v102 Feature Gate And Schema

**Files:**
- Modify: `plugins/violation_record/config.py`
- Create: `plugins/violation_record/policy_schema.py`
- Create: `tests/test_policy_schema.py`

- [ ] **Step 1: Write failing schema tests**

```python
def test_v102_schema_is_namespaced_and_idempotent(self):
    ensure_v102_schema(self.conn)
    ensure_v102_schema(self.conn)
    names = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    self.assertTrue(REQUIRED_V102_TABLES <= names)
    self.assertNotIn("member_policy_state", REQUIRED_V102_TABLES)

def test_operation_count_constraint_rejects_six(self):
    with self.assertRaises(sqlite3.IntegrityError):
        self.conn.execute("INSERT INTO v102_policy_state(member_id,group_area,v102_operation_count,created_at,updated_at) VALUES(1,'蜂巢',6,?,?)", (NOW, NOW))
```

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_policy_schema -v`

Expected: import failure because `policy_schema` does not exist.

- [ ] **Step 3: Implement exact schema**

Create `v102_policy_events`, `v102_policy_cycles`, `v102_policy_state`, `v102_pending_actions`, `v102_notification_outbox`, `v102_migration_checkpoints`, and `v102_baseline_audit`. Add unique idempotency keys and indexes for event ordering, source records, due cycles, pending reminders and outbox delivery. Every connection enables `foreign_keys`, `busy_timeout=5000`, row factory and explicit close.

- [ ] **Step 4: Verify GREEN and legacy isolation**

Run: `python -m unittest tests.test_policy_schema -v`

Expected: all schema tests pass and legacy table contents remain unchanged.

- [ ] **Step 5: Commit**

```bash
git add plugins/violation_record/config.py plugins/violation_record/policy_schema.py tests/test_policy_schema.py
git commit -m "feat: add isolated v102 policy schema"
```

### Task 2: Mute Severity And Count Adapter

**Files:**
- Create: `plugins/violation_record/deduction_policy.py`
- Modify: `plugins/violation_record/db.py`
- Create: `tests/test_deduction_policy.py`

- [ ] **Step 1: Write failing severity and count tests**

```python
def test_mute_duration_distinguishes_light_severe_and_unknown(self):
    self.assertEqual(parse_mute_seconds("禁言10分钟"), 600)
    self.assertEqual(parse_mute_seconds("禁言一小时"), 3600)
    self.assertEqual(classify_severity("警告"), Severity.NONE)
    self.assertEqual(classify_severity("禁言"), Severity.UNKNOWN)

def test_total_uses_baseline_adjustment(self):
    self.assertEqual(effective_total(self.conn, self.member_id, "蜂巢"), 7)
```

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_deduction_policy.SeverityTests tests.test_deduction_policy.CountAdapterTests -v`

Expected: missing parser and baseline adapter failures.

- [ ] **Step 3: Implement parser and single count formula**

Support Arabic and Chinese minute/hour expressions. Use `raw_effective_total + baseline_adjustment`, clamp at zero, preserve `deduct_count`, and route `_current_count()` and `_sync_state_counts()` through this adapter. Unknown countable duration creates a data-review pending action and blocks automatic settlement; warnings remain non-countable.

- [ ] **Step 4: Verify GREEN and query contracts**

Run: `python -m unittest tests.test_deduction_policy tests.test_query_contract tests.test_format_correction -v`

Expected: parser/count tests and existing query/format contracts pass.

- [ ] **Step 5: Commit**

```bash
git add plugins/violation_record/deduction_policy.py plugins/violation_record/db.py tests/test_deduction_policy.py
git commit -m "feat: add v102 count and severity model"
```

### Task 3: Normal And Progressive Slow Cycles

**Files:**
- Modify: `plugins/violation_record/deduction_policy.py`
- Modify: `tests/test_deduction_policy.py`

- [ ] **Step 1: Write failing timeline tests**

Cover first mute starting 14 days, current count 3 entering slow, slow levels 21/28/35 days, second light extending seven days, third light creating a stop suggestion, severe violation creating a suggestion, warnings having no policy effect, and identical-time violation priority before settlement.

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_deduction_policy.NormalCycleTests tests.test_deduction_policy.SlowCycleTests -v`

Expected: missing transition and due-settlement failures.

- [ ] **Step 3: Implement deterministic transitions**

Use `(effective_time, event_priority, source_sequence)` ordering. Persist every transition and cycle. Normal-good and slow-good settle `requested_amount=1`; increment `v102_operation_count` only when `applied_amount>0`; stop starting new reduction cycles at 5 operations and set `no_cycle_reason=operation_limit`.

- [ ] **Step 4: Verify GREEN and idempotency**

Run each timeline twice and assert one settlement event and one applied reduction.

- [ ] **Step 5: Commit**

```bash
git add plugins/violation_record/deduction_policy.py tests/test_deduction_policy.py
git commit -m "feat: implement normal and slow deduction cycles"
```

### Task 4: Stop Cycles And Final Warning

**Files:**
- Modify: `plugins/violation_record/deduction_policy.py`
- Modify: `tests/test_deduction_policy.py`

- [ ] **Step 1: Write failing stop/final-warning tests**

Cover fixed 30-day endpoints, hourly pending decision, delayed decisions that do not move endpoints, good release with reduction 1, bad release rejection, renewal from prior endpoint, final-warning 90-day recovery with requested reduction 2, final-warning violation creating remove-member pending action, and current count 0/1 recovery anomaly.

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_deduction_policy.StopCycleTests tests.test_deduction_policy.FinalWarningTests -v`

- [ ] **Step 3: Implement stop and final-warning reducers**

Represent `stop` and `final_warning` as different cycle types even though both project `policy_tag=stop`. General stop commands reject final-warning cycles. Pending decisions never auto-decide. Terminal member statuses stop cycles but preserve state and history.

- [ ] **Step 4: Verify GREEN and fixed endpoint invariant**

Assert every stop cycle start equals the previous fixed due time, including multi-endpoint delays.

- [ ] **Step 5: Commit**

```bash
git add plugins/violation_record/deduction_policy.py tests/test_deduction_policy.py
git commit -m "feat: implement stop and final warning policy"
```

### Task 5: Withdrawal, Backfill And Replay Boundary

**Files:**
- Modify: `plugins/violation_record/deduction_policy.py`
- Modify: `tests/test_deduction_policy.py`

- [ ] **Step 1: Write failing replay tests**

Test withdrawal restoring tags, level, due time, applied reduction and operation count; explicitly linked manual actions replaying through `caused_by_event_id`; unrelated manual actions remaining; recent-cycle backfill recomputing forward; old backfill only increasing count; and replay stopping at a manual closure.

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_deduction_policy.ReplayTests -v`

- [ ] **Step 3: Implement effective-event replay**

Retain original events for audit, mark reversals with `reversed_by_event_id`, rebuild only the affected member-group projection, and use `replay_generation` plus settlement idempotency keys. Never infer manual causality from time proximity.

- [ ] **Step 4: Verify GREEN and deterministic replay**

Compare canonical JSON projection after repeated replay and assert byte-for-byte equality.

- [ ] **Step 5: Commit**

```bash
git add plugins/violation_record/deduction_policy.py tests/test_deduction_policy.py
git commit -m "feat: add deterministic policy replay"
```

### Task 6: Fixed Management Commands

**Files:**
- Create: `plugins/violation_record/policy_commands.py`
- Create: `tests/test_policy_commands.py`
- Modify: `plugins/violation_record/formatter.py`

- [ ] **Step 1: Write failing parser and authorization tests**

Test exact commands `减停`, `清除减停`, `续期减停`, `拒绝减停建议`, `查询减数状态`, `查询减缓名单`, `查询减停名单`, `查询减停建议名单`, `查询减数待办`, and `查询减数日志`. Require group area, numeric QQ and non-empty reason for writes. Reject final-warning cycles and terminal statuses. Preserve operator-isolated confirmation. List and pending queries must return complete, deterministically ordered results and must not silently truncate.

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_policy_commands -v`

- [ ] **Step 3: Implement deterministic command routing**

Parse fixed commands without NLP. Return `None` for all non-policy text so the existing NLP path remains unchanged. Write operations return the existing confirm/cancel wording and store namespaced pending operation types.

- [ ] **Step 4: Verify GREEN and help output**

Run command tests plus `tests.test_format_correction` and assert existing help examples remain present.

- [ ] **Step 5: Commit**

```bash
git add plugins/violation_record/policy_commands.py plugins/violation_record/formatter.py tests/test_policy_commands.py
git commit -m "feat: add fixed deduction policy commands"
```

### Task 7: Existing Business Flow Integration

**Files:**
- Modify: `plugins/violation_record/service.py`
- Modify: `plugins/violation_record/matcher.py`
- Create: `tests/test_policy_integration.py`

- [ ] **Step 1: Write failing integration tests**

Test that confirmed create commits the original record even if policy processing fails, compensation processes it once later, withdrawal/status/consultation create policy events after their original transactions, non-policy messages still call `parse_intent()`, policy commands never call it, and non-target events execute no custom side effects.

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_policy_integration -v`

- [ ] **Step 3: Add post-commit bridge and compensation watermark**

Keep all existing user-facing return strings. Capture IDs/results internally, close the original transaction, then call the policy bridge. On failure log a sanitized warning and leave the source record committed for compensation scanning.

- [ ] **Step 4: Verify GREEN and full business contracts**

Run: `python -m unittest tests.test_policy_integration tests.test_query_contract tests.test_reply_delivery tests.test_evidence_service tests.test_format_correction -v`

- [ ] **Step 5: Commit**

```bash
git add plugins/violation_record/service.py plugins/violation_record/matcher.py tests/test_policy_integration.py
git commit -m "feat: integrate policy with existing record flow"
```

### Task 8: Offline Scheduler And Notification Outbox

**Files:**
- Modify: `plugins/violation_record/scheduler.py`
- Create: `tests/test_policy_scheduler.py`

- [ ] **Step 1: Write failing scheduler tests**

Assert due work executes with no connected bot, notifications remain queued offline, reconnect sends each once, hourly reminder slots are idempotent, startup creates one task, and shutdown cancels and awaits it.

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_policy_scheduler -v`

- [ ] **Step 3: Split rule execution from delivery**

Always run v102 maintenance when enabled. Only consume outbox when OneBot is connected. Keep old maintenance exclusively behind the disabled flag. Use a module task handle and `on_shutdown` cleanup.

- [ ] **Step 4: Verify GREEN and restart recovery**

Recreate scheduler state from the same SQLite file and assert no missed or duplicated settlement/reminder.

- [ ] **Step 5: Commit**

```bash
git add plugins/violation_record/scheduler.py tests/test_policy_scheduler.py
git commit -m "feat: decouple policy scheduler from NapCat"
```

### Task 9: Weekly Report And Audit Query

**Files:**
- Modify: `plugins/violation_record/exporter.py`
- Modify: `tests/test_policy_integration.py`

- [ ] **Step 1: Write failing report test**

Generate a weekly workbook and assert the original sheets remain plus a policy audit sheet containing automatic and manual operations, reason, actor, target, before/after and timestamps.

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_policy_integration.WeeklyPolicyReportTests -v`

- [ ] **Step 3: Add policy audit sheet without changing existing rows**

Append a new sheet only when v102 tables exist. Do not expose private baseline source paths or production configuration.

- [ ] **Step 4: Verify GREEN and openpyxl readability**

Load the generated workbook with `openpyxl.load_workbook(..., read_only=True)` and assert required headers.

- [ ] **Step 5: Commit**

```bash
git add plugins/violation_record/exporter.py tests/test_policy_integration.py
git commit -m "feat: include policy audit in weekly report"
```

### Task 10: Baseline Migration CLI

**Files:**
- Create: `scripts/migrate_v102.py`
- Create: `tests/test_v102_migration.py`

- [ ] **Step 1: Write failing dry-run/apply/rollback tests**

Create a small workbook fixture with main and excluded sheets. Test 127-style extraction rules, same QQ across areas, duplicate-scope failure, excluded conflict failure, no nickname overwrite, no synthetic violation rows, preserved old `deduct_count`, source SHA, cutover watermark, idempotent second dry-run, and logical rollback preserving post-cutover records.

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_v102_migration -v`

- [ ] **Step 3: Implement explicit modes**

```text
python scripts/migrate_v102.py --database DB --baseline XLSX --dry-run
python scripts/migrate_v102.py --database DB --baseline XLSX --snapshot-database BACKUP_DB --backup-sha256 SHA --apply
python scripts/migrate_v102.py --database DB --batch-id BATCH --logical-rollback
python scripts/migrate_v102.py --database DB --verify
```

`--dry-run` opens the database read-only. `--apply` validates the real pre-cutover backup file and its SHA-256, compares the backup with the locked live business data, records `cutover_at` and watermark, writes audit rows, applies the unified baseline adjustment and initializes cycles. Legacy candidate databases must complete `--repair-snapshots` before runtime readiness or `--logical-rollback` is allowed. `--logical-rollback` reverses v102 settlements/projection while preserving source records created after cutover.

- [ ] **Step 4: Verify GREEN and database invariants**

Run migration twice on a copy, verify `integrity_check`, `foreign_key_check`, count equations, one active cycle per member-area, operation count bounds, tag exclusivity and unique idempotency keys.

- [ ] **Step 5: Commit**

```bash
git add scripts/migrate_v102.py tests/test_v102_migration.py
git commit -m "feat: add guarded v102 baseline migration"
```

### Task 11: Complete Acceptance And Public Boundaries

**Files:**
- Modify: `tests/test_public_source.py`
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `CHANGELOG.md`
- Create: `docs/releases/2026-08-02-v1.0.2beta.md`

- [ ] **Step 1: Add acceptance and public-scan tests**

Encode the PRD mathematical invariants, 29 timeline cases, replay/backfill cases, target-group isolation, fixed-command validation and migration exclusions. Scan tracked files for production IDs, credentials, absolute private paths, XLSX data and backup names.

- [ ] **Step 2: Verify RED where behavior is missing**

Run: `python -m unittest discover -s tests -v`

- [ ] **Step 3: Complete only missing behavior and Chinese release docs**

Document the beta status, management commands, rollback flag and known testing scope without disclosing production timing/configuration values.

- [ ] **Step 4: Verify full suite and static checks**

```bash
python -m unittest discover -s tests -v
python -m compileall -q bot.py plugins scripts tests
bash -n scripts/*.sh
pip check
python scripts/check_public_tree.py .
git diff --check
```

- [ ] **Step 5: Commit**

```bash
git add .env.example CHANGELOG.md README.md docs/releases/2026-08-02-v1.0.2beta.md tests/test_public_source.py
git commit -m "docs: prepare v1.0.2beta testing release"
```

### Task 12: Copy Validation, Production Cutover And Rollback Gate

**Files:**
- No source edits during cutover.

- [ ] **Step 1: Freeze a clean squash commit and create a new rollback snapshot**

From current `origin/main`, squash the fully tested implementation into one sanitized release commit. Never push the `feat/impl` history. Record the clean release commit, `ff62a53` rollback commit, dependency freeze, service state, systemd files, mode-600 `.env`, source archive, three databases and evidence directory. Compute SHA-256 without printing secrets.

- [ ] **Step 2: Rehearse both the legacy upgrade and a fresh migration**

On one copy of the current production database, run `beta-1 snapshot repair -> verify -> cancel baseline-only notifications -> logical rollback -> verify`. On a second copy of the exact pre-cutover database, run `apply -> verify -> logical rollback -> verify`. Do not continue unless all invariants pass and the original source records/evidence remain.

- [ ] **Step 3: Execute production maintenance window**

Stop watchdog and daily-restart timers, keep `qq-violation-bot.service` stopped, confirm NapCat remains available, and take a final root-only online backup. Switch the production worktree to the exact clean release commit without touching ignored runtime data. Because production already contains a `beta-1` checkpoint, run the attested `--repair-snapshots` path, verify `v1.0.2beta-2`, cancel only the 53 baseline-initialization outbox rows, enable the v102 flag and then start the bot.

- [ ] **Step 4: Verify live health without fabricating violations**

Require service active, `NRestarts=0`, plugin loaded, port 6199 established, NapCat active, database integrity and v102 invariants clean, migration checkpoint applied, no duplicate idempotency key, no stuck outbox item, and existing read-only query behavior intact. Restore timers only after all checks pass.

- [ ] **Step 5: Version management**

After live health checks pass, push only the clean release commit to public `main`, create and push the beta tag `v1.0.2beta`, and publish the Chinese GitHub release notes. Never push `feat/v1.0.2beta-deduction-policy` or `impl/v1.0.2beta-deduction-policy`; those histories contain private runtime identifiers. Do not create `v1.1.0`. Keep the SSH session open for long-term testing follow-up.

## Self-Review Gate

- Every PRD section maps to at least one task above.
- New tables and migration IDs are namespaced; legacy experimental rows are untouched.
- Every production behavior starts with a failing test and observed RED result.
- All existing query, record, evidence and reply contract tests run in every integration gate.
- No plan step requires production data in Git or fabricates a live violation.
- The rollback path changes after the bot resumes writes and never overwrites post-cutover data.
