# CArroT / kona Dual-Instance Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run CArroT and kona as isolated QQ bot instances on one server, with kona permanently chat-only and a gated CArroT-to-kona promotion workflow.

**Architecture:** Introduce an explicit instance root for every mutable/runtime path and an immutable `BOT_MODE` capability ceiling. Deploy commit-addressed releases behind per-instance symlinks, separate both NoneBot and NapCat processes/configuration, and require a real @ of the target bot for group administration. CArroT accepts server-only candidate commits; only CArroT-approved commits enter GitHub `main` and become manually promotable to kona.

**Tech Stack:** Python 3.10+, NoneBot2, OneBot V11, NapCat/Electron, SQLite, systemd, Bash, GitHub Actions, `unittest`.

---

Before each local test command, prepare a synthetic group ID that does not occur in the tracked tree:

```bash
TEST_TARGET_GROUP_ID=$(python3 -c 'import secrets; print(secrets.randbelow(900000000)+100000000)')
while git grep -q "$TEST_TARGET_GROUP_ID"; do
  TEST_TARGET_GROUP_ID=$(python3 -c 'import secrets; print(secrets.randbelow(900000000)+100000000)')
done
export TEST_TARGET_GROUP_ID
```

### Task 1: Instance-rooted configuration and persona

**Files:**
- Create: `plugins/runtime_paths.py`
- Modify: `bot.py`
- Modify: `plugins/violation_record/config.py`
- Modify: `plugins/random_chat/persona.py`
- Modify: `.env.example`
- Create: `tests/test_instance_config.py`
- Modify: `tests/test_character_prompt.py`

- [ ] **Step 1: Write failing subprocess tests for isolated roots**

Add tests that launch a clean Python subprocess with a temporary `BOT_INSTANCE_ROOT`, a valid synthetic `TARGET_GROUP_ID`, and no repository `.env` dependency:

```python
def test_instance_root_owns_every_mutable_path(self):
    payload = run_config_probe(
        BOT_INSTANCE_ROOT=str(self.root),
        TARGET_GROUP_ID=str(10**8 + 73_185_296),
    )
    self.assertEqual(str(self.root / "data"), payload["data_dir"])
    self.assertEqual(str(self.root / "backups"), payload["backup_dir"])
    self.assertEqual(str(self.root / "character.md"), payload["character_file"])
    for value in payload["mutable_paths"]:
        self.assertTrue(Path(value).is_relative_to(self.root))

def test_two_instance_roots_never_resolve_same_runtime_file(self):
    carrot = run_config_probe(BOT_INSTANCE_ROOT=str(self.root / "carrot"))
    kona = run_config_probe(BOT_INSTANCE_ROOT=str(self.root / "kona"))
    self.assertNotEqual(carrot["chat_archive_path"], kona["chat_archive_path"])
    self.assertNotEqual(carrot["runtime_features_path"], kona["runtime_features_path"])
    self.assertNotEqual(carrot["sticker_root"], kona["sticker_root"])
```

- [ ] **Step 2: Run the tests and confirm RED**

Run:

```bash
TARGET_GROUP_ID="$TEST_TARGET_GROUP_ID" .venv/bin/python -m unittest \
  tests.test_instance_config tests.test_character_prompt -v
```

Expected: failures show paths still resolve under the code checkout and `character.md` ignores `BOT_INSTANCE_ROOT`.

- [ ] **Step 3: Implement explicit code and instance roots**

Create a side-effect-free `plugins.runtime_paths` module and use it from `bot.py`, `config.py` and `persona.py`. It preserves the code root but derives mutable paths from an absolute instance root:

```python
CODE_ROOT = Path(__file__).resolve().parents[1]
_raw_instance_root = Path(os.getenv("BOT_INSTANCE_ROOT", str(CODE_ROOT)))
INSTANCE_ROOT = Path(os.path.abspath(_raw_instance_root))
DATA_DIR = INSTANCE_ROOT / "data"
EXPORT_DIR = INSTANCE_ROOT / "exports"
BACKUP_DIR = INSTANCE_ROOT / "backups"
LOG_DIR = INSTANCE_ROOT / "logs"
```

Relative runtime settings, including `DATABASE_URL` defaults and `CHAT_VISION_IMAGE_ROOT`, must resolve below `INSTANCE_ROOT`; static Python imports still resolve from `CODE_ROOT`. Reject a configured instance root that is not absolute after normalization or whose final path is a symbolic link.

In `bot.py`, load the instance `.env` before `nonebot.init()`:

```python
CODE_ROOT = Path(__file__).resolve().parent
INSTANCE_ROOT = Path(os.environ.get("BOT_INSTANCE_ROOT", CODE_ROOT)).resolve()
load_dotenv(INSTANCE_ROOT / ".env")
```

Expose `CONFIG.character_file = INSTANCE_ROOT / "character.md"` and make `load_character_prompt()` read that path at call time. Do not cache either instance's character text.

- [ ] **Step 4: Run focused tests and compile**

Run the Step 2 command plus:

```bash
.venv/bin/python -m compileall -q bot.py plugins/violation_record plugins/random_chat
```

Expected: all focused tests pass; both instance probes return disjoint paths.

- [ ] **Step 5: Commit**

```bash
git add plugins/runtime_paths.py bot.py plugins/violation_record/config.py plugins/random_chat/persona.py \
  .env.example tests/test_instance_config.py tests/test_character_prompt.py
git commit -m "feat: isolate bot runtime by instance root"
```

### Task 2: Immutable `chat_only` business capability ceiling

**Files:**
- Modify: `plugins/violation_record/config.py`
- Modify: `plugins/violation_record/__init__.py`
- Modify: `plugins/feature_control/state.py`
- Modify: `plugins/feature_control/runtime.py`
- Modify: `plugins/feature_control/commands.py`
- Modify: `plugins/group_router/matcher.py`
- Modify: `.env.example`
- Create: `tests/test_bot_mode.py`
- Modify: `tests/test_feature_control.py`
- Modify: `tests/test_feature_control_commands.py`
- Modify: `tests/test_group_router.py`
- Modify: `tests/test_plugin_loading.py`

- [ ] **Step 1: Write failing capability tests**

Cover both modes:

```python
def test_chat_only_has_no_business_target_requirement(self):
    result = run_config_probe(BOT_MODE="chat_only", TARGET_GROUP_ID=None)
    self.assertEqual("chat_only", result["bot_mode"])
    self.assertEqual(0, result["target_group_id"])

def test_chat_only_refuses_runtime_business_enable(self):
    controller = controller_for_mode("chat_only")
    with self.assertRaisesRegex(ValueError, "chat-only"):
        controller.set_switch("business_enabled", True, "100")
    self.assertFalse(controller.business_allowed(123, 123))

def test_chat_only_does_not_register_business_scheduler(self):
    loaded = load_plugins_in_mode("chat_only")
    self.assertNotIn("violation-policy-maintenance", loaded.startup_task_names)
```

Also assert `/业务 开` and `/模型网关 业务 开` return an unavailable response without state mutation, while `full` retains existing behavior.

- [ ] **Step 2: Run and confirm RED**

```bash
TARGET_GROUP_ID="$TEST_TARGET_GROUP_ID" .venv/bin/python -m unittest \
  tests.test_bot_mode tests.test_feature_control \
  tests.test_feature_control_commands tests.test_group_router \
  tests.test_plugin_loading -v
```

Expected: `BOT_MODE` is unknown, chat-only still requires a target group, and business can be enabled.

- [ ] **Step 3: Implement the capability ceiling**

Validate mode once:

```python
def _bot_mode_env() -> Literal["full", "chat_only"]:
    value = str(os.getenv("BOT_MODE", "full")).strip().lower()
    if value not in {"full", "chat_only"}:
        raise RuntimeError("BOT_MODE must be full or chat_only")
    return cast(Literal["full", "chat_only"], value)
```

For `chat_only`, use target group `0`, force business defaults and Gateway business defaults false, make `FeatureController` carry `business_capable=False`, and reject any transition that enables either business switch. `business_allowed()` must always return false when incapable.

Only call `setup_scheduler()` from `plugins.violation_record.__init__` in `full` mode. Keep helper imports available because the group chat router uses message-direction helpers, but do not initialize the business database or maintenance loop in chat-only mode.

Return explicit status text:

```text
业务功能：不可用（纯聊天实例）
```

- [ ] **Step 4: Run focused and adjacent business regression tests**

Run Step 2, then:

```bash
TARGET_GROUP_ID="$TEST_TARGET_GROUP_ID" .venv/bin/python -m unittest \
  tests.test_violation_chat_fallback tests.test_policy_scheduler \
  tests.test_llm_gateway_business_migration -v
```

Expected: chat-only tests pass and full-mode business tests remain unchanged.

- [ ] **Step 5: Commit**

```bash
git add plugins/violation_record/config.py plugins/violation_record/__init__.py \
  plugins/feature_control/state.py plugins/feature_control/runtime.py \
  plugins/feature_control/commands.py plugins/group_router/matcher.py \
  .env.example tests/test_bot_mode.py tests/test_feature_control.py \
  tests/test_feature_control_commands.py tests/test_group_router.py \
  tests/test_plugin_loading.py
git commit -m "feat: add permanent chat-only instance mode"
```

### Task 3: Target-specific group administration

**Files:**
- Create: `plugins/feature_control/addressing.py`
- Modify: `plugins/feature_control/matcher.py`
- Modify: `plugins/memory_governance/matcher.py`
- Modify: `tests/test_feature_control_commands.py`
- Modify: `tests/test_memory_governance_matcher.py`

- [ ] **Step 1: Write real OneBot segment RED tests**

Construct group events for two bot IDs and assert only the explicitly mentioned bot accepts the command:

```python
message = Message([
    MessageSegment.at("20002"),
    MessageSegment.text(" /提示构建 关"),
])
self.assertFalse(await accepts_group_admin(event(self_id="10001", message=message)))
self.assertTrue(await accepts_group_admin(event(self_id="20002", message=message)))
```

Cover no `at`, `at('all')`, two bot mentions, a text-only fake `@20002`, both superusers, and private commands without an `at`. Repeat the addressing matrix for memory governance.

- [ ] **Step 2: Run and confirm RED**

```bash
TARGET_GROUP_ID="$TEST_TARGET_GROUP_ID" .venv/bin/python -m unittest \
  tests.test_feature_control_commands tests.test_memory_governance_matcher -v
```

Expected: both group matchers currently accept the same plain command without checking `self_id`.

- [ ] **Step 3: Add a shared addressing rule**

Implement a pure helper that inspects real OneBot segments:

```python
def group_admin_targets_self(event: GroupMessageEvent) -> bool:
    targets = [
        str(segment.data.get("qq", ""))
        for segment in event.message
        if segment.type == "at"
    ]
    return targets == [str(event.self_id)]
```

For group events, both matchers must require this helper before parsing or reading runtime state. For private events, preserve current behavior. Remove only the target bot's `at` segment when constructing command text; never trust display text as an identity signal.

- [ ] **Step 4: Run focused and plugin-loading tests**

Run Step 2 and:

```bash
TARGET_GROUP_ID="$TEST_TARGET_GROUP_ID" .venv/bin/python -m unittest tests.test_plugin_loading -v
```

Expected: all addressing, authorization-order and loading tests pass.

- [ ] **Step 5: Commit**

```bash
git add plugins/feature_control/addressing.py plugins/feature_control/matcher.py \
  plugins/memory_governance/matcher.py tests/test_feature_control_commands.py \
  tests/test_memory_governance_matcher.py
git commit -m "fix: target group administration to one bot"
```

### Task 4: Release and instance deployment tooling

**Files:**
- Create: `scripts/deploy_instance.py`
- Create: `scripts/instance_health.py`
- Create: `deploy/systemd/qqbot@.service`
- Modify: `scripts/start_bot.sh`
- Create: `tests/test_deploy_instance.py`
- Create: `tests/test_instance_health.py`

- [ ] **Step 1: Write filesystem-only RED tests**

Use temporary fake Git releases and service runners. Test allowlisted instance names, full 40-character SHAs, lock exclusion, immutable release creation, per-instance `current` symlink switching, failed health rollback, successful promotion and retention that never deletes a referenced release.

```python
def test_failed_health_restores_only_target_instance(self):
    deploy("carrot", NEW_SHA, health_exit=1)
    self.assertEqual(OLD_SHA, current_sha("carrot"))
    self.assertEqual(KONA_SHA, current_sha("kona"))
```

- [ ] **Step 2: Run and confirm RED**

```bash
TARGET_GROUP_ID="$TEST_TARGET_GROUP_ID" .venv/bin/python -m unittest \
  tests.test_deploy_instance tests.test_instance_health -v
```

Expected: deployment modules and systemd template do not exist.

- [ ] **Step 3: Implement strict deployment and health commands**

`deploy_instance.py` accepts only `--instance carrot|kona`, `--sha <40 lowercase hex>`, a canonical deployment root, and a canonical bare/source Git repository. It acquires `flock`, verifies the commit exists, exports tracked files, creates a release-local virtual environment, installs the locked project requirements, compiles the release, atomically changes only the requested instance symlink, restarts that service, and invokes health verification. Any failure restores the old symlink and restarts the old release.

`instance_health.py` verifies:

- systemd service is active;
- configured loopback port is listening;
- an established loopback OneBot connection exists for that port;
- the instance's `current` resolves to the requested SHA;
- recent service logs contain no traceback/critical startup failure;
- persisted feature state is parseable and kona remains business-incapable.

The systemd template uses `Environment=BOT_INSTANCE_ROOT=/opt/qq-bots/instances/%i`, an instance-specific `EnvironmentFile`, the instance `current` release, restart limits and a bounded stop timeout.

- [ ] **Step 4: Run focused tests, shell syntax and compile**

```bash
TARGET_GROUP_ID="$TEST_TARGET_GROUP_ID" .venv/bin/python -m unittest \
  tests.test_deploy_instance tests.test_instance_health -v
bash -n scripts/start_bot.sh
.venv/bin/python -m compileall -q scripts/deploy_instance.py scripts/instance_health.py
```

Expected: all checks pass without requiring systemd or root in unit tests.

- [ ] **Step 5: Commit**

```bash
git add scripts/deploy_instance.py scripts/instance_health.py scripts/start_bot.sh \
  deploy/systemd/qqbot@.service tests/test_deploy_instance.py \
  tests/test_instance_health.py
git commit -m "feat: deploy isolated bot releases atomically"
```

### Task 5: Isolated NapCat instances

**Files:**
- Modify: `scripts/start_napcat.sh`
- Create: `deploy/systemd/napcat@.service`
- Create: `tests/test_start_napcat.py`

- [ ] **Step 1: Write command-construction RED tests**

Test two instance roots produce different `HOME`, `XDG_CONFIG_HOME`, QQ data paths, tokens, bot IDs and reverse WebSocket ports. Reject missing env files, unsafe instance names, reused ports and invalid QQ IDs.

- [ ] **Step 2: Run and confirm RED**

```bash
TARGET_GROUP_ID="$TEST_TARGET_GROUP_ID" .venv/bin/python -m unittest tests.test_start_napcat -v
```

Expected: the existing script hard-codes `/opt/qq-violation-bot` and `/root/Napcat`.

- [ ] **Step 3: Parameterize NapCat startup**

Use the systemd instance name (`carrot` or `kona`) to derive its canonical `/opt/qq-bots/instances/carrot` or `/opt/qq-bots/instances/kona` root. Read only that instance's `.env`, set its dedicated `HOME` and `XDG_CONFIG_HOME`, and launch the common NapCat/QQ binary with an instance-specific Electron user-data directory. The template service must have its own restart policy and may not bind public ports.

- [ ] **Step 4: Run tests and syntax validation**

```bash
TARGET_GROUP_ID="$TEST_TARGET_GROUP_ID" .venv/bin/python -m unittest tests.test_start_napcat -v
bash -n scripts/start_napcat.sh
```

Expected: all isolation tests and shell parsing pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/start_napcat.sh deploy/systemd/napcat@.service \
  tests/test_start_napcat.py
git commit -m "feat: isolate napcat runtime per bot"
```

### Task 6: Idempotent Swap provisioning

**Files:**
- Create: `scripts/provision_swap.sh`
- Create: `tests/test_provision_swap.py`
- Modify: `README.md`

- [ ] **Step 1: Write RED tests using fake command adapters**

Test first apply, repeated apply, insufficient disk, an existing unrelated swap, wrong `/swapfile` type or permissions, exact fstab/sysctl managed blocks, verification failure rollback and explicit removal.

- [ ] **Step 2: Run and confirm RED**

```bash
TARGET_GROUP_ID="$TEST_TARGET_GROUP_ID" .venv/bin/python -m unittest tests.test_provision_swap -v
```

Expected: provisioning script is missing.

- [ ] **Step 3: Implement strict apply/status/remove operations**

The script uses `set -euo pipefail`, an exact `/swapfile` target, a 2 GiB default, `0600`, `mkswap`, `swapon`, one marked `/etc/fstab` entry, one marked `/etc/sysctl.d/99-qq-bots-swap.conf` setting `vm.swappiness=10`, and post-change checks. `remove` requires explicit action, runs `swapoff` first, then removes only managed entries and the validated regular file.

- [ ] **Step 4: Run tests and shell syntax**

```bash
TARGET_GROUP_ID="$TEST_TARGET_GROUP_ID" .venv/bin/python -m unittest tests.test_provision_swap -v
bash -n scripts/provision_swap.sh
```

Expected: all tests pass and repeated status/apply is clean.

- [ ] **Step 5: Commit**

```bash
git add scripts/provision_swap.sh tests/test_provision_swap.py README.md
git commit -m "ops: add idempotent swap provisioning"
```

### Task 7: CI and gated promotion

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/promote-kona.yml`
- Create: `scripts/deploy_carrot_candidate.sh`
- Create: `tests/test_deployment_workflows.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Write workflow-policy RED tests**

Parse workflow YAML and scripts to assert:

- `main` CI runs full `unittest`, compile and both public scans;
- kona uses `workflow_dispatch`, a protected `kona-production` environment and an exact main SHA;
- no workflow auto-deploys kona on push;
- CArroT candidate script pushes only the server remote candidate ref and never GitHub;
- deploy secrets are referenced by name and never embedded;
- kona deploy calls the server deploy tool with `--instance kona`.

- [ ] **Step 2: Run and confirm RED**

```bash
TARGET_GROUP_ID="$TEST_TARGET_GROUP_ID" .venv/bin/python -m unittest \
  tests.test_deployment_workflows -v
```

Expected: workflow files and candidate deployment script are absent.

- [ ] **Step 3: Implement workflows and candidate script**

`ci.yml` runs on `main` and uses a generated synthetic group ID that is not tracked in the repository. `promote-kona.yml` accepts a full SHA, verifies it equals an ancestor/current approved `main` commit with successful CI, uses GitHub environment approval, installs a pinned SSH host key, and calls the restricted server deployment command.

`deploy_carrot_candidate.sh` requires a clean local worktree and full local commit, stores `git rev-parse HEAD` in `CANDIDATE_SHA`, runs focused/full gates, pushes only `HEAD:release/carrot-candidate` to the server remote, then invokes `deploy_instance.py --instance carrot --sha "$CANDIDATE_SHA"` over SSH.

- [ ] **Step 4: Run workflow, syntax and public-source checks**

```bash
TARGET_GROUP_ID="$TEST_TARGET_GROUP_ID" .venv/bin/python -m unittest \
  tests.test_deployment_workflows tests.test_public_source tests.test_public_scanner -v
bash -n scripts/deploy_carrot_candidate.sh
.venv/bin/python scripts/check_public_tree.py
```

Expected: all tests and scans pass; no private value appears in tracked files.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml .github/workflows/promote-kona.yml \
  scripts/deploy_carrot_candidate.sh tests/test_deployment_workflows.py \
  README.md CHANGELOG.md
git commit -m "ci: gate carrot candidates and kona promotion"
```

### Task 8: Complete local verification

**Files:**
- Modify only if a verification failure exposes a scoped defect.

- [ ] **Step 1: Run focused dual-instance suites**

```bash
TARGET_GROUP_ID="$TEST_TARGET_GROUP_ID" .venv/bin/python -m unittest \
  tests.test_instance_config tests.test_bot_mode \
  tests.test_feature_control_commands tests.test_memory_governance_matcher \
  tests.test_deploy_instance tests.test_instance_health \
  tests.test_start_napcat tests.test_provision_swap \
  tests.test_deployment_workflows -v
```

- [ ] **Step 2: Run the complete repository suite**

Generate a numeric `TARGET_GROUP_ID` not present in tracked files and run:

```bash
TARGET_GROUP_ID="$TEST_TARGET_GROUP_ID" .venv/bin/python -m unittest discover -s tests -v
```

Expected: zero failures and zero errors.

- [ ] **Step 3: Run compilation, diff and privacy gates**

```bash
.venv/bin/python -m compileall -q bot.py plugins scripts tests
git diff --check
.venv/bin/python scripts/check_public_tree.py
.venv/bin/python scripts/check_public_tree.py --history
git status --short
```

Expected: all commands exit zero and the worktree is clean after the verification commit.

- [ ] **Step 4: Resolve failures in their owning task**

If any command fails, return to the task that owns the failing file, add a focused RED regression, make the minimal fix, rerun that task's verification, and amend that task's commit before repeating Tasks 8 Steps 1–3. Do not create an aggregate catch-all verification commit.

### Task 9: Provision Swap and migrate CArroT safely

**Files:**
- Server only: `/etc/fstab`, `/etc/sysctl.d/99-qq-bots-swap.conf`, `/swapfile`, `/opt/qq-bots/instances/carrot/`, systemd templates.

- [ ] **Step 1: Capture read-only production baseline**

Record current Git SHA/branch, clean status, systemd units, QQ self ID, OneBot connection, memory/disk, database quick checks, runtime feature state hashes and canonical paths. Stop if the worktree is dirty or any database quick check fails.

- [ ] **Step 2: Create verified backups**

Use the existing online SQLite backup mechanisms for databases and a root-only archive for `.env`, `character.md`, runtime JSON, memory mirrors and NapCat configuration. Store a manifest with SHA-256 checksums and `0600` files in a `0700` backup directory. Do not print secret contents.

- [ ] **Step 3: Provision and verify 2 GiB Swap**

Run:

```bash
sudo scripts/provision_swap.sh apply --size-gib 2 --swappiness 10
sudo scripts/provision_swap.sh status
```

Expected: `/swapfile` is active, exactly one managed fstab entry exists, swappiness is 10, and CArroT/OneBot remain online.

- [ ] **Step 4: Install release/instance structure and migrate CArroT**

Create canonical directories and service accounts, install the tested release, copy CArroT mutable state into `instances/carrot` while stopped, verify ownership and checksums, install systemd templates, then start `qqbot@carrot` and `napcat@carrot`. Keep the old services disabled but intact until acceptance.

- [ ] **Step 5: Validate CArroT and rollback path**

Verify exact SHA, service health, port 6199, bot self ID, OneBot connection, runtime switches, character loading, database quick checks, group chat, private chat and one reversible runtime switch. Exercise a code-pointer rollback to the prior release and forward again without restoring data. Stop if any checksum or identity differs.

### Task 10: Initialize kona and dual-instance acceptance

**Files:**
- Server only: `/opt/qq-bots/instances/kona/`, kona systemd/NapCat state, private GitHub environment secrets.

- [ ] **Step 1: Create an empty kona instance**

Create `0700` instance directories and `0600` `.env` from a redacted template. Set `BOT_MODE=chat_only`, port 6299, kona QQ self ID, separate NapCat token/API Key, both superusers, independent group/private allowlists, and all rollout switches initially off. Install the user-provided kona `character.md`. Leave the sticker directory empty and sticker sending disabled.

- [ ] **Step 2: Start isolated kona NapCat and obtain QR login**

Start only `napcat@kona`, expose its console through an authenticated loopback SSH tunnel if required, and let the user scan the QR code. Confirm the connected QQ self ID equals the configured kona ID before starting the bot.

- [ ] **Step 3: Initialize databases and enable chat features incrementally**

Start `qqbot@kona`, run schema quick checks, confirm business is unavailable, then enable chat total/group/private allowlists, image understanding, member/private memory, relationship, governance, Gateway and Prompt Builder in that order. Keep sticker sending disabled until kona assets are added.

- [ ] **Step 4: Prove cross-instance isolation**

Capture database watermarks and directory manifests for both instances. Send a group message and a private message to one bot at a time, then assert only the targeted instance changes for private data and runtime commands. In a shared group, verify both archives remain separate files, exact @ replies target one bot, and `@CArroT /提示构建 关` does not mutate kona (and vice versa).

- [ ] **Step 5: Configure gated GitHub promotion**

Create the restricted deployment SSH key, pin the server host key, add GitHub environment secrets without exposing values, require manual approval for `kona-production`, and dry-run an already deployed SHA. Confirm workflow logs contain only instance name, SHA, stage and result.

- [ ] **Step 6: Final acceptance and handoff**

Report both instance SHAs, systemd/NapCat/OneBot status, ports, memory/Swap usage, business capability, data roots, rollback SHAs and exact operational commands. Do not report keys, tokens, private QQ allowlists or chat contents.
