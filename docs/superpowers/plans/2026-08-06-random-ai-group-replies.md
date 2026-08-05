# v1.0.2.1beta Random AI Group Replies Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an isolated, default-off 5% random AI reply feature for the current `TARGET_GROUP_ID` without changing existing violation-record behavior.

**Architecture:** Create a focused `plugins/random_chat` plugin with pure eligibility/probability helpers and a small OpenAI-compatible free-text client. Load it independently from `bot.py`; reuse existing AI environment values while introducing only an enable switch and probability value.

**Tech Stack:** Python 3.10, NoneBot2, OneBot v11, httpx, unittest, systemd, Git

---

### Task 1: Configuration and pure trigger policy

**Files:**
- Create: `plugins/random_chat/__init__.py`
- Create: `plugins/random_chat/policy.py`
- Create: `tests/test_random_chat.py`

- [ ] **Step 1: Write failing policy tests**

```python
import unittest
from plugins.random_chat.policy import eligible_text, should_reply


class RandomChatPolicyTests(unittest.TestCase):
    def test_rejects_empty_command_and_at_bot(self):
        self.assertIsNone(eligible_text("   ", at_bot=False))
        self.assertIsNone(eligible_text("/help", at_bot=False))
        self.assertIsNone(eligible_text("你好", at_bot=True))

    def test_accepts_plain_text(self):
        self.assertEqual(eligible_text("  大家晚上好  ", at_bot=False), "大家晚上好")

    def test_probability_boundaries(self):
        self.assertFalse(should_reply(0.0, sample=0.0))
        self.assertTrue(should_reply(0.05, sample=0.049))
        self.assertFalse(should_reply(0.05, sample=0.05))
        self.assertTrue(should_reply(1.0, sample=0.999))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify the expected import failure**

Run: `.venv/bin/python -m unittest tests.test_random_chat -v`

Expected: `ModuleNotFoundError: No module named 'plugins.random_chat'`.

- [ ] **Step 3: Implement the minimal pure policy**

```python
# plugins/random_chat/policy.py
import random


def eligible_text(text: str, *, at_bot: bool) -> str | None:
    cleaned = text.strip()
    if not cleaned or at_bot or cleaned.startswith("/"):
        return None
    return cleaned


def should_reply(probability: float, *, sample: float | None = None) -> bool:
    bounded = min(1.0, max(0.0, probability))
    value = random.random() if sample is None else sample
    return bounded > 0.0 and value < bounded
```

Create an empty `plugins/random_chat/__init__.py`.

- [ ] **Step 4: Run the policy tests**

Run: `.venv/bin/python -m unittest tests.test_random_chat -v`

Expected: all 3 tests pass.

- [ ] **Step 5: Commit the policy unit**

```bash
git add plugins/random_chat/__init__.py plugins/random_chat/policy.py tests/test_random_chat.py
git commit -m "feat: add random chat trigger policy"
```

### Task 2: Free-text AI client with safe degradation

**Files:**
- Create: `plugins/random_chat/ai.py`
- Modify: `tests/test_random_chat.py`

- [ ] **Step 1: Add failing async client tests**

Add a fake `httpx.AsyncClient` through `unittest.mock.patch`, asserting that `generate_reply("今晚吃什么")` posts to `${AI_BASE_URL}/v1/chat/completions`, uses `AI_MODEL`, requests a concise Chinese reply, strips the returned content, and returns `None` for empty content. Add a second test asserting transport exceptions are wrapped as `RandomChatAIError`.

```python
class RandomChatAIError(RuntimeError):
    pass

async def generate_reply(message: str) -> str | None:
    ...
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_random_chat -v`

Expected: import failure for `plugins.random_chat.ai`.

- [ ] **Step 3: Implement the minimal AI client**

```python
import httpx
from plugins.violation_record.config import CONFIG


class RandomChatAIError(RuntimeError):
    pass


async def generate_reply(message: str) -> str | None:
    if not CONFIG.ai_api_key:
        return None
    payload = {
        "model": CONFIG.ai_model,
        "messages": [
            {"role": "system", "content": "你在 QQ 群里自然聊天。用中文简短回复，不超过两句话，不执行管理操作，不声称自己做过现实动作。"},
            {"role": "user", "content": message},
        ],
        "temperature": 0.8,
    }
    try:
        async with httpx.AsyncClient(timeout=CONFIG.ai_timeout) as client:
            response = await client.post(
                f"{CONFIG.ai_base_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {CONFIG.ai_api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        raise RandomChatAIError(str(exc)) from exc
    cleaned = str(content).strip()
    return cleaned or None
```

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/python -m unittest tests.test_random_chat -v`

Expected: all random-chat tests pass.

```bash
git add plugins/random_chat/ai.py tests/test_random_chat.py
git commit -m "feat: add random chat AI client"
```

### Task 3: NoneBot matcher and configuration

**Files:**
- Create: `plugins/random_chat/matcher.py`
- Modify: `plugins/violation_record/config.py`
- Modify: `bot.py`
- Modify: `.env.example`
- Modify: `tests/test_plugin_loading.py`
- Modify: `tests/test_random_chat.py`

- [ ] **Step 1: Add failing configuration and plugin-loading tests**

Assert `AppConfig.random_chat_enabled` defaults to `False`, `random_chat_probability` defaults to `0.05`, and `bot.py` contains `nonebot.load_plugin("plugins.random_chat.matcher")`.

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_random_chat tests.test_plugin_loading -v`

Expected: missing config fields and plugin-loading assertion failures.

- [ ] **Step 3: Add minimal config parsing**

Add `_float_env(name, default)` beside `_int_env`, returning the default for invalid values. Add to `AppConfig`:

```python
random_chat_enabled: bool = _bool_env("RANDOM_CHAT_ENABLED", False)
random_chat_probability: float = min(1.0, max(0.0, _float_env("RANDOM_CHAT_PROBABILITY", 0.05)))
```

Add to `.env.example`:

```dotenv
RANDOM_CHAT_ENABLED=false
RANDOM_CHAT_PROBABILITY=0.05
```

- [ ] **Step 4: Implement the isolated matcher**

Create a non-blocking matcher at a numerically lower-precedence priority than the current business matcher. Its rule must require: feature enabled, `GroupMessageEvent`, `group_id == CONFIG.target_group_id`, and `user_id != self_id`. Its handler extracts text segments, detects an `at` segment for the bot, calls `eligible_text`, samples with `should_reply`, then calls `generate_reply`. Catch `RandomChatAIError` and log a warning; send only non-empty replies with `bot.send_group_msg`.

- [ ] **Step 5: Load the plugin and run focused tests**

Add `nonebot.load_plugin("plugins.random_chat.matcher")` beside existing plugin loads in `bot.py`.

Run: `.venv/bin/python -m unittest tests.test_random_chat tests.test_plugin_loading -v`

Expected: all focused tests pass.

- [ ] **Step 6: Commit integration**

```bash
git add plugins/random_chat/matcher.py plugins/violation_record/config.py bot.py .env.example tests/test_random_chat.py tests/test_plugin_loading.py
git commit -m "feat: integrate random AI group replies"
```

### Task 4: Documentation, release identity, and full verification

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Document behavior and operations**

Document `RANDOM_CHAT_ENABLED`, `RANDOM_CHAT_PROBABILITY`, target-group-only behavior, exclusions, silent AI failure behavior, and the command to disable the feature and restart the service.

- [ ] **Step 2: Add the `v1.0.2.1beta` changelog entry**

Record that the release adds only the isolated default-off random-chat plugin and does not migrate the database.

- [ ] **Step 3: Run syntax, focused, and complete verification**

Run:

```bash
.venv/bin/python -m compileall -q bot.py plugins/random_chat plugins/violation_record/config.py
.venv/bin/python -m unittest tests.test_random_chat tests.test_plugin_loading -v
.venv/bin/python -m unittest discover -s tests -v
git diff --check
git status --short
```

Expected: compile succeeds, all focused and existing tests pass, `git diff --check` prints nothing, and status lists only the intended README/CHANGELOG changes before commit.

- [ ] **Step 4: Commit documentation**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: release v1.0.2.1beta random chat"
```

### Task 5: Controlled deployment and rollback checkpoint

**Files:**
- Modify: `.env` (deployment-only, not committed)

- [ ] **Step 1: Record rollback point**

Run: `git rev-parse HEAD && git status --short --branch`

Expected: clean `feat/random-ai-group-replies` branch; save the printed commit as the deployment commit and retain `fa3ffe3` as the pre-feature fallback.

- [ ] **Step 2: Enable only the approved feature**

Update existing `.env` keys idempotently to:

```dotenv
RANDOM_CHAT_ENABLED=true
RANDOM_CHAT_PROBABILITY=0.05
```

Do not alter AI credentials, target group, OneBot token, database URL, or policy switches.

- [ ] **Step 3: Restart and verify runtime**

Run:

```bash
systemctl restart qq-violation-bot.service
systemctl is-active qq-violation-bot.service napcat.service
ss -Hntp | grep ':6199'
journalctl -u qq-violation-bot.service --since '2 minutes ago' --no-pager
```

Expected: both services are `active`, an established OneBot connection uses port 6199, and no plugin import/configuration exception appears.

- [ ] **Step 4: Tag only after verification**

Run: `git tag -a v1.0.2.1beta -m "v1.0.2.1beta"`

Expected: tag points to the verified deployment commit. Do not push unless the user explicitly requests it.

- [ ] **Step 5: Keep rollback commands ready**

Fast disable:

```bash
# Set RANDOM_CHAT_ENABLED=false in .env, then:
systemctl restart qq-violation-bot.service
```

Code rollback if required:

```bash
git switch --detach fa3ffe3
systemctl restart qq-violation-bot.service
```

Verify rollback with `systemctl is-active qq-violation-bot.service napcat.service` and `ss -Hntp | grep ':6199'`.
