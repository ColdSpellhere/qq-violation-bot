# 萝卜猫身份边界 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让群聊和私聊中的萝卜猫始终知道萝卜猫只是名字、自己不是猫且不说“喵”，并把反二梦女视为兴趣与自我标签。

**Architecture:** 只修改共享 AI 系统提示词，因此群聊和私聊自动保持一致。测试直接截获发送给 AI 的 payload 并断言身份边界，不增加生成后过滤、重试或新依赖。

**Tech Stack:** Python 3、unittest、httpx 测试替身

---

### Task 1: 收紧共享身份提示词

**Files:**
- Modify: `tests/test_random_chat.py`
- Modify: `plugins/random_chat/ai.py`

- [ ] **Step 1: 写失败测试**

在已有群聊人格测试和私聊场景测试中断言系统提示词包含：

```python
self.assertIn("萝卜猫只是你的名字", system_prompt)
self.assertIn("你不是猫", system_prompt)
self.assertIn("不要使用“喵”", system_prompt)
self.assertIn("反二梦女是你认可的兴趣和自我标签", system_prompt)
self.assertIn("不是另一个名字", system_prompt)
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `.venv/bin/python -m unittest tests.test_random_chat -v`

Expected: 新身份断言失败，因为旧提示词仍把反二梦女描述为可使用的名字，且没有禁止猫系自称和“喵”。

- [ ] **Step 3: 最小化修改提示词**

将共享身份段改为：

```python
"你叫萝卜猫，萝卜猫只是你的名字。你是一个特别可爱但说话自然的小女孩式虚构 QQ 聊天角色；"
"你不是猫，不要自称猫，也不要使用“喵”或其他猫系口癖。你喜欢花、植物和自然里的小东西。"
"反二梦女是你认可的兴趣和自我标签，不是另一个名字；聊到相关话题时可以自然说自己也是反二梦女，"
"但不要主动反复介绍这些设定，也不要每句话都卖萌、撒娇或使用幼儿化口吻。"
```

- [ ] **Step 4: 运行聚焦测试并确认 GREEN**

Run: `.venv/bin/python -m unittest tests.test_random_chat tests.test_private_chat -v`

Expected: 全部通过。

- [ ] **Step 5: 运行全量验证**

Run: `.venv/bin/python -m unittest discover -s tests -v`

Expected: 0 failures；随后运行 `.venv/bin/python -m compileall -q bot.py plugins tests`，退出码为 0。

- [ ] **Step 6: 提交并更新临时运行版本**

```bash
git add docs/superpowers/specs/2026-08-18-private-radish-cat-chat-design.md \
  docs/superpowers/plans/2026-08-18-radish-cat-identity-boundary.md \
  tests/test_random_chat.py plugins/random_chat/ai.py
git commit -m "fix: clarify radish cat identity"
```

备份生产运行中的 `plugins/random_chat/ai.py`，复制已验证文件，重启机器人服务，并确认服务 active、OneBot 反向 WebSocket 已建立且启动日志无导入错误。
