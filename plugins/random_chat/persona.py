from pathlib import Path

from nonebot import logger

from plugins.runtime_paths import CHARACTER_FILE

DEFAULT_CHARACTER_PROMPT = """# 萝卜猫角色设定

你叫萝卜猫，萝卜猫只是你的名字。你是一个特别可爱但说话自然的小女孩式虚构 QQ 聊天角色。

你不是猫，不要自称猫，也不要使用“喵”或其他猫系口癖。你喜欢花和植物，也喜欢自然里的小东西。

反二梦女是你认可的兴趣和自我标签，不是另一个名字。聊到相关话题时，可以自然说自己也是反二梦女，但不要主动反复介绍这些设定，也不要每句话都卖萌、撒娇或使用幼儿化口吻。"""


def load_character_prompt(path: Path | None = None) -> str:
    target = CHARACTER_FILE if path is None else path
    try:
        content = target.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        logger.warning(f"角色设定读取失败，使用内置默认值：{type(exc).__name__}")
        return DEFAULT_CHARACTER_PROMPT
    if not content:
        logger.warning("角色设定为空，使用内置默认值")
        return DEFAULT_CHARACTER_PROMPT
    return content
