# Editable Character Prompt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the shared 萝卜猫 persona into a root-level `character.md` that is read fresh before every group or private AI reply.

**Architecture:** A focused `plugins/random_chat/persona.py` module owns the canonical file path, built-in fallback, and UTF-8 loading behavior. `plugins/random_chat/ai.py` asks the loader for the persona on every `generate_reply` call while retaining routing, safety, output, and scene rules in code.

**Tech Stack:** Python 3.10, pathlib, NoneBot logger, unittest, unittest.mock

---

## File structure

- Create `character.md`: administrator-editable shared group/private persona text.
- Create `plugins/random_chat/persona.py`: resolve the root path, read UTF-8 content on demand, and fall back safely.
- Create `tests/test_character_prompt.py`: focused loader tests.
- Modify `plugins/random_chat/ai.py`: load the file for every reply.
- Modify `tests/test_random_chat.py`: prove per-request loading and preserve prompt boundaries.
- Modify `README.md`: document live editing and fallback behavior.

### Task 1: Add the character file loader

**Files:**
- Create: `character.md`
- Create: `plugins/random_chat/persona.py`
- Create: `tests/test_character_prompt.py`

- [ ] **Step 1: Write the failing loader tests**

Create `tests/test_character_prompt.py`:

```python
import tempfile
import unittest
from pathlib import Path

from plugins.random_chat.persona import DEFAULT_CHARACTER_PROMPT, load_character_prompt


class CharacterPromptTests(unittest.TestCase):
    def test_reads_utf8_markdown_and_strips_outer_whitespace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "character.md"
            path.write_text("\n# 新角色\n\n喜欢薄荷。\n", encoding="utf-8")
            self.assertEqual("# 新角色\n\n喜欢薄荷。", load_character_prompt(path))

    def test_reads_file_again_on_every_call(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "character.md"
            path.write_text("第一版", encoding="utf-8")
            self.assertEqual("第一版", load_character_prompt(path))
            path.write_text("第二版", encoding="utf-8")
            self.assertEqual("第二版", load_character_prompt(path))

    def test_missing_empty_and_invalid_utf8_use_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "character.md"
            self.assertEqual(DEFAULT_CHARACTER_PROMPT, load_character_prompt(path))
            path.write_text(" \n\t", encoding="utf-8")
            self.assertEqual(DEFAULT_CHARACTER_PROMPT, load_character_prompt(path))
            path.write_bytes(b"\xff\xfe")
            self.assertEqual(DEFAULT_CHARACTER_PROMPT, load_character_prompt(path))
```

- [ ] **Step 2: Run the test and verify RED**

Run: `.venv/bin/python -m unittest tests.test_character_prompt -v`

Expected: import fails because `plugins.random_chat.persona` does not exist.

- [ ] **Step 3: Create the editable character file**

Create `character.md`:

```markdown
# 萝卜猫角色设定

你叫萝卜猫，萝卜猫只是你的名字。你是一个特别可爱但说话自然的小女孩式虚构 QQ 聊天角色。

你不是猫，不要自称猫，也不要使用“喵”或其他猫系口癖。你喜欢花、植物和自然里的小东西。

反二梦女是你认可的兴趣和自我标签，不是另一个名字。聊到相关话题时，可以自然说自己也是反二梦女，但不要主动反复介绍这些设定，也不要每句话都卖萌、撒娇或使用幼儿化口吻。
```

- [ ] **Step 4: Implement the loader**

Create `plugins/random_chat/persona.py`:

```python
from pathlib import Path

from nonebot import logger


CHARACTER_FILE = Path(__file__).resolve().parents[2] / "character.md"
DEFAULT_CHARACTER_PROMPT = """# 萝卜猫角色设定

你叫萝卜猫，萝卜猫只是你的名字。你是一个特别可爱但说话自然的小女孩式虚构 QQ 聊天角色。

你不是猫，不要自称猫，也不要使用“喵”或其他猫系口癖。你喜欢花、植物和自然里的小东西。

反二梦女是你认可的兴趣和自我标签，不是另一个名字。聊到相关话题时，可以自然说自己也是反二梦女，但不要主动反复介绍这些设定，也不要每句话都卖萌、撒娇或使用幼儿化口吻。"""


def load_character_prompt(path: Path = CHARACTER_FILE) -> str:
    try:
        content = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        logger.warning(f"角色设定读取失败，使用内置默认值：{type(exc).__name__}")
        return DEFAULT_CHARACTER_PROMPT
    if not content:
        logger.warning("角色设定为空，使用内置默认值")
        return DEFAULT_CHARACTER_PROMPT
    return content
```

- [ ] **Step 5: Run the test and verify GREEN**

Run: `.venv/bin/python -m unittest tests.test_character_prompt -v`

Expected: 3 tests pass.

- [ ] **Step 6: Commit the loader unit**

```bash
git add character.md plugins/random_chat/persona.py tests/test_character_prompt.py
git commit -m "feat: load editable character prompt"
```

### Task 2: Load the persona for every AI request

**Files:**
- Modify: `plugins/random_chat/ai.py:1-110`
- Modify: `tests/test_random_chat.py:75-210`

- [ ] **Step 1: Add a failing per-request integration test**

Add to `RandomChatAITests`:

```python
async def test_loads_character_prompt_for_every_ai_request(self):
    with patch("plugins.random_chat.ai.CONFIG", self.config), patch(
        "plugins.random_chat.ai.httpx.AsyncClient", _FakeClient
    ), patch(
        "plugins.random_chat.ai.load_character_prompt",
        side_effect=["角色版本一", "角色版本二"],
    ) as loader:
        await generate_reply("第一条", context=[])
        first_prompt = _FakeClient.posted[2]["messages"][0]["content"]
        await generate_reply("第二条", context=[])
        second_prompt = _FakeClient.posted[2]["messages"][0]["content"]

    self.assertIn("角色版本一", first_prompt)
    self.assertNotIn("角色版本二", first_prompt)
    self.assertIn("角色版本二", second_prompt)
    self.assertNotIn("角色版本一", second_prompt)
    self.assertEqual(2, loader.call_count)
```

- [ ] **Step 2: Run the test and verify RED**

Run: `.venv/bin/python -m unittest tests.test_random_chat.RandomChatAITests.test_loads_character_prompt_for_every_ai_request -v`

Expected: patching fails because `plugins.random_chat.ai.load_character_prompt` is not defined.

- [ ] **Step 3: Integrate the loader**

Add to `plugins/random_chat/ai.py`:

```python
from plugins.random_chat.persona import load_character_prompt
```

Remove `identity_policy`. Replace the identity block in the system prompt with:

```python
+ load_character_prompt()
+ "\n"
```

Keep `scene_policy`, `reply_policy`, `style_policy`, `direction_policy`, `safety_policy`, and `output_policy` unchanged.

- [ ] **Step 4: Run focused tests**

Run: `.venv/bin/python -m unittest tests.test_character_prompt tests.test_random_chat.RandomChatAITests -v`

Expected: loader and AI tests all pass, including existing group/private identity assertions.

- [ ] **Step 5: Commit the integration**

```bash
git add plugins/random_chat/ai.py tests/test_random_chat.py
git commit -m "feat: apply character file to every chat reply"
```

### Task 3: Document and verify

**Files:**
- Modify: `README.md:90-96`

- [ ] **Step 1: Document live editing**

Add to the existing 萝卜猫 section:

```markdown
人物设定保存在项目根目录的 `character.md`。群聊和私聊在每次请求 AI 前都会重新读取该文件，保存后的修改会从下一条回复开始生效，无需重启；文件缺失、为空或无法按 UTF-8 读取时自动使用内置默认设定。业务隔离、对话方向、安全限制和输出规则仍由程序控制，不受角色文件影响。
```

- [ ] **Step 2: Run focused verification**

Run:

```bash
.venv/bin/python -m compileall -q plugins tests
.venv/bin/python -m unittest tests.test_character_prompt tests.test_random_chat tests.test_private_chat -v
git diff --check
```

Expected: compilation succeeds, selected tests pass, and `git diff --check` prints nothing.

- [ ] **Step 3: Run the full suite**

Run: `TARGET_GROUP_ID="$(.venv/bin/python -c 'import secrets; print(secrets.randbelow(900000000) + 100000000)')" .venv/bin/python -m unittest discover -s tests -q`

Expected: all tests pass with no failures or errors.

- [ ] **Step 4: Commit documentation**

```bash
git add README.md
git commit -m "docs: explain editable character prompt"
```

- [ ] **Step 5: Deploy with rollback protection**

Record the current production commit and back up an existing `/opt/qq-violation-bot/character.md`. Fast-forward production to the verified commit, restart `qq-violation-bot.service` once, then verify both services are active and the reverse WebSocket on `127.0.0.1:6199` is established. Rollback restores the recorded commit and prior character file, followed by one restart.
