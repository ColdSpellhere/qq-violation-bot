# Prompt Builder Speaker Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every group-chat message and member-memory entry carry a deterministic request-local speaker reference so the model cannot casually merge different QQ users.

**Architecture:** Add a small pure speaker-mapping module, carry speaker references through the existing budget model, and render a compact speaker directory plus strict ownership rules. Keep the legacy prompt, storage, routing, probability, private/group isolation, and Gateway transport unchanged.

**Tech Stack:** Python 3.10+, dataclasses, unittest, existing NoneBot2/OneBot V11 models and Prompt Builder.

---

## File map

- Create `plugins/chat_prompt/speakers.py`: deterministic request-local speaker directory and lookup helpers; no NoneBot, database, or Gateway imports.
- Modify `plugins/chat_prompt/models.py`: immutable speaker identity type and parallel speaker-reference fields in budgeted data.
- Modify `plugins/chat_prompt/budget.py`: assign references, keep reference arrays aligned with context/fact trimming, charge directory characters to the total budget, and prune unused speakers.
- Modify `plugins/chat_prompt/builder.py`: render the directory, speaker-linked history/facts/current message, and fixed ownership rules.
- Create `tests/test_chat_prompt_speakers.py`: focused identity, pronoun, quote, nickname, missing-ID, and budget integrity regressions.
- Modify `tests/test_chat_prompt_budget.py`: preserve existing truncation guarantees with directory overhead.
- Modify `tests/test_llm_gateway_chat_migration.py`: prove Builder-off legacy equivalence and Builder-on group/private integration boundaries.
- Modify `README.md` and `CHANGELOG.md`: document the corrected speaker contract and rollout/stop-loss behavior.

### Task 1: Deterministic request-local speaker directory

**Files:**
- Create: `plugins/chat_prompt/speakers.py`
- Modify: `plugins/chat_prompt/models.py`
- Test: `tests/test_chat_prompt_speakers.py`

- [ ] **Step 1: Write failing identity tests**

Create focused tests that construct two `ContextMessage` objects and assert exact behavior:

```python
def test_same_qq_reuses_ref_across_nickname_change(self):
    directory = build_speaker_directory(
        current=self.turn("200", "新昵称", "现在"),
        context=(self.turn("200", "旧昵称", "之前"),),
        profiles=(),
    )
    assert directory.ref_for_user("200") == "S1"
    assert len([item for item in directory.identities if item.user_id == "200"]) == 1

def test_same_nickname_different_qq_gets_distinct_refs(self):
    directory = build_speaker_directory(
        current=self.turn("200", "同名", "当前"),
        context=(self.turn("100", "同名", "历史"),),
        profiles=(),
    )
    assert directory.ref_for_user("200") != directory.ref_for_user("100")
```

Also assert current user is `S1`, referenced/mentioned known QQ users receive stable refs, and two missing-ID turns do not merge into the current user.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
TEST_TARGET_GROUP_ID=$(python3 -c 'import secrets; print(secrets.randbelow(900000000)+100000000)')
while git grep -q "$TEST_TARGET_GROUP_ID"; do
  TEST_TARGET_GROUP_ID=$(python3 -c 'import secrets; print(secrets.randbelow(900000000)+100000000)')
done
TARGET_GROUP_ID="$TEST_TARGET_GROUP_ID" \
  .venv/bin/python -m unittest tests.test_chat_prompt_speakers -v
```

Expected: `ImportError` for `plugins.chat_prompt.speakers` or missing speaker types, proving the new contract does not yet exist.

- [ ] **Step 3: Implement the pure speaker map**

Add immutable types in `models.py`:

```python
@dataclass(frozen=True)
class SpeakerIdentity:
    ref: str
    user_id: str
    nickname: str
    current: bool = False

@dataclass(frozen=True)
class SpeakerDirectory:
    identities: tuple[SpeakerIdentity, ...]
    refs_by_user: tuple[tuple[str, str], ...]

    def ref_for_user(self, user_id: str) -> str | None:
        return next((ref for value, ref in self.refs_by_user if value == user_id), None)
```

Implement `build_speaker_directory()` in `speakers.py` with this deterministic order:

1. current sender;
2. current reply author and @ targets;
3. history in chronological order;
4. profiles.

Use exact non-empty `user_id` as the reuse key. Give the current sender `S1`; allocate subsequent known users as `S2`, `S3`, and so on. Allocate unknown turns using `U1`, `U2` keyed by message ID; never reuse `S1` for unknown data. Prefer the current nickname for the current QQ and retain the first non-empty nickname for other users.

- [ ] **Step 4: Run tests and verify GREEN**

Run the Task 1 command again. Expected: all focused speaker-directory tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add plugins/chat_prompt/speakers.py plugins/chat_prompt/models.py \
  tests/test_chat_prompt_speakers.py
git commit -m "feat: add deterministic chat speaker directory"
```

### Task 2: Carry speaker ownership through budgets and rendering

**Files:**
- Modify: `plugins/chat_prompt/models.py`
- Modify: `plugins/chat_prompt/budget.py`
- Modify: `plugins/chat_prompt/builder.py`
- Modify: `tests/test_chat_prompt_speakers.py`
- Modify: `tests/test_chat_prompt_budget.py`

- [ ] **Step 1: Write failing rendering and budget tests**

Add tests that parse the rendered messages and assert:

```python
def test_first_person_stays_with_history_author(self):
    prompt = build_chat_prompt(self.input(
        current=self.turn("200", "乙", "他说的花是什么？"),
        context=(self.turn("100", "甲", "我喜欢养花"),),
    ))
    system = prompt.messages[0]["content"]
    user = prompt.messages[1]["content"]
    assert "第一人称" in system
    assert "CURRENT=S1" in user
    assert "speaker_ref=S2" in user
    assert "我喜欢养花" in user
    assert "speaker_ref=S1" in user

def test_reply_author_does_not_replace_current_sender(self):
    prompt = build_chat_prompt(self.input(
        current=self.turn("200", "乙", "这句话呢", replied_to_user_id="100"),
        context=(self.turn("100", "甲", "我喜欢养花"),),
    ))
    assert '"current_speaker_ref":"S1"' in prompt.messages[1]["content"]
    assert '"reply_author_ref":"S2"' in prompt.messages[1]["content"]
```

Add a member-memory test proving a profile for QQ `100` uses the same ref as QQ `100` history, never the current QQ `200`. Add an escape-heavy 12000-character budget test that asserts every referenced `speaker_ref` has exactly one directory entry and `prompt.total_chars <= 12000`.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
TEST_TARGET_GROUP_ID=$(python3 -c 'import secrets; print(secrets.randbelow(900000000)+100000000)')
while git grep -q "$TEST_TARGET_GROUP_ID"; do
  TEST_TARGET_GROUP_ID=$(python3 -c 'import secrets; print(secrets.randbelow(900000000)+100000000)')
done
TARGET_GROUP_ID="$TEST_TARGET_GROUP_ID" .venv/bin/python -m unittest \
  tests.test_chat_prompt_speakers tests.test_chat_prompt_budget -v
```

Expected: failures because current rendering lacks the speaker directory, ownership rules, and aligned reference fields.

- [ ] **Step 3: Add aligned budget fields and pruning**

Extend `BudgetedPromptData` with immutable fields:

```python
speakers: tuple[SpeakerIdentity, ...]
context_speaker_refs: tuple[str, ...]
fact_speaker_refs: tuple[str, ...]
current_speaker_ref: str
current_at_speaker_refs: tuple[str, ...]
current_reply_author_ref: str | None
```

In `apply_prompt_budget()`:

- build the directory once from the typed source;
- render each context/fact with its matching ref;
- whenever context/facts are sliced or removed, slice/remove the parallel ref tuple in the same operation;
- after all trimming, retain only current, @, reply, remaining context, and remaining fact refs;
- charge compact directory line lengths to `prompt_data_chars()`;
- never remove the current identity or current message.

Use a helper with a fail-closed invariant:

```python
def _prune_speakers(data: BudgetedPromptData) -> BudgetedPromptData:
    used = {
        data.current_speaker_ref,
        *data.current_at_speaker_refs,
        *data.context_speaker_refs,
        *data.fact_speaker_refs,
    }
    if data.current_reply_author_ref:
        used.add(data.current_reply_author_ref)
    return replace(data, speakers=tuple(item for item in data.speakers if item.ref in used))
```

- [ ] **Step 4: Render compact speaker-linked sections**

In `builder.py`, add fixed system rules that state exact author ownership, local first-person semantics, QQ/ref-only memory joins, current sender semantics, and fail-closed uncertainty. Render:

```text
<speaker_directory_data>
S1|qq=200|nickname=乙|current=true
S2|qq=100|nickname=甲
</speaker_directory_data>
```

Render history/facts with `speaker_ref=...`, and add `current_speaker_ref`, `at_speaker_refs`, and `reply_author_ref` to current-message JSON. Keep all sections in the untrusted `user` message; do not move persona or memory into `system`.

- [ ] **Step 5: Run focused and adjacent tests**

Run:

```bash
TEST_TARGET_GROUP_ID=$(python3 -c 'import secrets; print(secrets.randbelow(900000000)+100000000)')
while git grep -q "$TEST_TARGET_GROUP_ID"; do
  TEST_TARGET_GROUP_ID=$(python3 -c 'import secrets; print(secrets.randbelow(900000000)+100000000)')
done
TARGET_GROUP_ID="$TEST_TARGET_GROUP_ID" .venv/bin/python -m unittest \
  tests.test_chat_prompt_speakers \
  tests.test_chat_prompt_budget \
  tests.test_random_chat_context \
  tests.test_llm_gateway_chat_migration -v
```

Expected: all tests pass; legacy migration tests remain unchanged.

- [ ] **Step 6: Commit Task 2**

```bash
git add plugins/chat_prompt/models.py plugins/chat_prompt/budget.py \
  plugins/chat_prompt/builder.py tests/test_chat_prompt_speakers.py \
  tests/test_chat_prompt_budget.py
git commit -m "fix: bind chat context to exact speakers"
```

### Task 3: Integration boundaries, documentation, and release verification

**Files:**
- Modify: `tests/test_llm_gateway_chat_migration.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add integration contract regressions**

Add tests that exercise `generate_reply()` with Builder on and assert the Gateway receives the speaker directory, exact current/ref authors, and profile-to-speaker binding. These assertions are the outer integration form of the Task 2 tests and should pass only after Task 2 is GREEN. Add a Builder-off test that compares the full legacy `messages` payload with the parent commit fixture. Add a private-mode test proving only the current private user appears and no group/private cross-scope data is added.

- [ ] **Step 2: Run integration tests and verify the completed boundary**

Run:

```bash
TEST_TARGET_GROUP_ID=$(python3 -c 'import secrets; print(secrets.randbelow(900000000)+100000000)')
while git grep -q "$TEST_TARGET_GROUP_ID"; do
  TEST_TARGET_GROUP_ID=$(python3 -c 'import secrets; print(secrets.randbelow(900000000)+100000000)')
done
TARGET_GROUP_ID="$TEST_TARGET_GROUP_ID" \
  .venv/bin/python -m unittest tests.test_llm_gateway_chat_migration -v
```

Expected: all integration assertions pass without a routing production edit. If routing changes appear necessary, stop and return to the approved design rather than expanding the fix inside the integration task.

- [ ] **Step 3: Document the runtime contract**

Update README Prompt Builder documentation to state that request-local speaker refs bind history, facts, @ and replies by exact QQ, first-person pronouns stay local to their author, and private/group isolation is unchanged. Add an Unreleased CHANGELOG bullet for the production attribution fix and the `/提示构建 关` stop-loss.

- [ ] **Step 4: Run full verification**

Run with a freshly generated nine-digit `TARGET_GROUP_ID` that does not occur in the repository:

```bash
TEST_TARGET_GROUP_ID=$(python3 -c 'import secrets; print(secrets.randbelow(900000000)+100000000)')
while git grep -q "$TEST_TARGET_GROUP_ID"; do
  TEST_TARGET_GROUP_ID=$(python3 -c 'import secrets; print(secrets.randbelow(900000000)+100000000)')
done
TARGET_GROUP_ID="$TEST_TARGET_GROUP_ID" .venv/bin/python -m unittest discover -s tests
python3 -m compileall -q bot.py plugins scripts tests
git diff --check
.venv/bin/python scripts/check_public_tree.py --history
git status --short
```

Expected: all tests pass with zero failures/errors; compile and diff checks are silent; public scan reports PASS; only intended files are modified.

- [ ] **Step 5: Commit documentation and integration tests**

```bash
git add tests/test_llm_gateway_chat_migration.py README.md CHANGELOG.md
git commit -m "docs: describe exact chat speaker ownership"
```

- [ ] **Step 6: Deploy with Prompt Builder still off**

Fast-forward the reviewed branch into GitHub `main`, push an independent server release branch, verify a clean production worktree and current rollback ref, then stop the service, switch code, compile, and restart. Do not migrate the database because this change has no schema impact. Confirm service active, OneBot connected, and Prompt Builder remains off.

- [ ] **Step 7: Re-enable and smoke test**

Enable `/提示构建 开` only after deployment. Test the fixed two-speaker corpus, same-nickname/different-QQ case, an @/reply case, a normal private reply, and Builder-off fallback. Inspect only status/count metadata in `llm_usage_events` and logs; do not print chat content. If attribution is still wrong, immediately execute `/提示构建 关` and retain Gateway chat mode.
