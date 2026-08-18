# Private Radish Cat Chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a default-off, single-account OneBot private-chat plugin that replies to every eligible text with the 萝卜猫 persona, keeps only 20 in-memory turns, and optionally attaches existing stickers.

**Architecture:** A dedicated `plugins/private_chat` package owns private-event filtering, serialized in-memory conversation state, AI orchestration, and private-message delivery. It reuses only the random-chat AI persona and sticker selector; group business, archive, and member-memory matchers remain group-only and unchanged.

**Tech Stack:** Python 3.10, NoneBot2, OneBot V11, unittest, systemd, Git.

---

### Task 1: Private Configuration And Event Policy

**Files:**
- Modify: `plugins/violation_record/config.py`
- Create: `plugins/private_chat/__init__.py`
- Create: `plugins/private_chat/policy.py`
- Modify: `.env.example`
- Test: `tests/test_private_chat.py`

- [ ] **Step 1: Write failing configuration and policy tests**

```python
def test_private_chat_defaults_are_safe():
    assert CONFIG.private_chat_enabled is False
    assert CONFIG.private_chat_allowed_user_id == ""

def test_policy_accepts_only_configured_human_private_sender():
    assert is_private_candidate(True, "123456", "123456", "999999")
    assert not is_private_candidate(True, "123456", "654321", "999999")
    assert not is_private_candidate(False, "123456", "123456", "999999")
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m unittest tests.test_private_chat -v`

Expected: import/configuration failure because `plugins.private_chat.policy` and the private configuration fields do not exist.

- [ ] **Step 3: Implement minimal configuration and policy**

```python
def is_private_candidate(enabled: bool, allowed_user_id: str, user_id: str, self_id: str) -> bool:
    allowed = str(allowed_user_id).strip()
    return enabled and allowed.isdigit() and str(user_id) == allowed and str(user_id) != str(self_id)
```

Add `private_chat_enabled` and `private_chat_allowed_user_id` to `AppConfig`; keep the public example disabled with an empty account value.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `.venv/bin/python -m unittest tests.test_private_chat -v`

Expected: all Task 1 tests pass.

### Task 2: Bounded Serialized Conversation State

**Files:**
- Create: `plugins/private_chat/conversation.py`
- Test: `tests/test_private_chat.py`

- [ ] **Step 1: Write failing state tests**

```python
def test_conversation_keeps_only_twenty_turns():
    conversation = PrivateConversation(limit=20)
    for index in range(21):
        conversation.append(ContextMessage(str(index), str(index), message_id=str(index), user_id=str(index)))
    assert len(conversation.snapshot()) == 20
    assert conversation.snapshot()[0].message_id == "1"

async def test_conversation_lock_serializes_handlers():
    conversation = PrivateConversation(limit=20)
    assert conversation.lock.locked() is False
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m unittest tests.test_private_chat.PrivateConversationTests -v`

Expected: import failure because `PrivateConversation` does not exist.

- [ ] **Step 3: Implement minimal bounded state**

```python
class PrivateConversation:
    def __init__(self, limit: int = 20):
        self._turns = deque(maxlen=limit)
        self.lock = asyncio.Lock()

    def append(self, turn: ContextMessage) -> None:
        self._turns.append(turn)

    def snapshot(self) -> tuple[ContextMessage, ...]:
        return tuple(self._turns)
```

- [ ] **Step 4: Run tests and verify GREEN**

Run: `.venv/bin/python -m unittest tests.test_private_chat.PrivateConversationTests -v`

Expected: bounded-state tests pass.

### Task 3: Private Persona Prompt

**Files:**
- Modify: `plugins/random_chat/ai.py`
- Modify: `tests/test_random_chat.py`

- [ ] **Step 1: Write a failing private-mode prompt test**

```python
async def test_private_mode_uses_one_to_one_prompt_without_group_language(self):
    await generate_reply("在吗", context=[], addressed=True, chat_mode="private")
    prompt = _FakeClient.posted[2]["messages"][0]["content"]
    assert "一对一 QQ 私聊" in prompt
    assert "QQ 群聊" not in prompt
    assert "群友" not in prompt
    assert "不要输出 SKIP" in prompt
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m unittest tests.test_random_chat.RandomChatAITests.test_private_mode_uses_one_to_one_prompt_without_group_language -v`

Expected: `generate_reply` rejects the unknown `chat_mode` argument.

- [ ] **Step 3: Add an explicit private scene branch**

Add `chat_mode: Literal["group", "private"] = "group"`. Compose private-only scene, direction, output, and history labels while preserving the exact existing group-mode prompt assertions.

- [ ] **Step 4: Run AI tests and verify GREEN**

Run: `.venv/bin/python -m unittest tests.test_random_chat -v`

Expected: existing group tests and the new private test all pass.

### Task 4: Isolated Private Matcher And Delivery

**Files:**
- Create: `plugins/private_chat/matcher.py`
- Modify: `bot.py`
- Modify: `tests/test_private_chat.py`
- Modify: `tests/test_plugin_loading.py`

- [ ] **Step 1: Write failing matcher tests**

```python
async def test_allowed_text_always_replies_with_private_mode_and_sticker():
    await handle_private_message(bot, private_event("你好"))
    generate.assert_awaited_once()
    assert generate.await_args.kwargs["addressed"] is True
    assert generate.await_args.kwargs["chat_mode"] == "private"
    bot.send_private_msg.assert_awaited_once()

async def test_empty_command_ai_and_send_failures_do_not_escape():
    await handle_private_message(bot, private_event("/help"))
    generate.assert_not_awaited()
```

Also assert the plugin-loading test contains `plugins.private_chat.matcher` and the candidate rule rejects every non-allowed sender.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m unittest tests.test_private_chat tests.test_plugin_loading -v`

Expected: matcher/plugin-loading failures because the plugin is not implemented or loaded.

- [ ] **Step 3: Implement the matcher**

Register an `on_message` matcher restricted to `PrivateMessageEvent`. Inside `CONVERSATION.lock`, append the user turn, call `generate_reply(..., addressed=True, chat_mode="private")`, select a sticker with the existing 20% configuration, send one `send_private_msg`, and append the bot turn only after successful delivery. Catch AI and send exceptions at the private-plugin boundary.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `.venv/bin/python -m unittest tests.test_private_chat tests.test_random_chat tests.test_plugin_loading -v`

Expected: all private, persona, and plugin-loading tests pass.

### Task 5: Release, Deployment, And Verification

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Create: `docs/releases/2026-08-18-v1.0.2.7beta.md`
- Modify privately on server: `.env`

- [ ] **Step 1: Document behavior and rollback**

Document default-off single-account private chat, in-memory-only 20-turn context, 20% sticker behavior, business isolation, and `PRIVATE_CHAT_ENABLED=false` rollback.

- [ ] **Step 2: Configure production privately**

Set `PRIVATE_CHAT_ENABLED=true` and `PRIVATE_CHAT_ALLOWED_USER_ID` to the user-supplied QQ only in `/opt/qq-violation-bot/.env`; never place the real value in tracked files or command output.

- [ ] **Step 3: Run complete verification**

Run the full unittest discovery with the private `TARGET_GROUP_ID` injected into the process, `compileall`, `git diff --check`, and `scripts/check_public_tree.py`.

Expected: zero failures, zero syntax errors, zero whitespace errors, and no private runtime values in tracked content.

- [ ] **Step 4: Deploy with rollback backup**

Back up `.env` and changed production files, copy the verified worktree changes into `/opt/qq-violation-bot`, restart only `qq-violation-bot.service`, and leave the connected NapCat session running.

- [ ] **Step 5: Verify production and publish**

Confirm both services are active, reverse WebSocket port `6199` is established, runtime private-chat configuration is enabled without printing the allowed QQ, and startup logs contain no import errors. Commit as `v1.0.2.7beta`, tag, and push the feature branch and tag.
