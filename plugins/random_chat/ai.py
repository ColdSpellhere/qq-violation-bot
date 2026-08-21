import httpx
from collections.abc import Sequence
from typing import Literal

from plugins.chat_archive.db import ContextMessage
from plugins.member_memory.store import MemberProfile
from plugins.random_chat.persona import load_character_prompt
from plugins.violation_record.config import CONFIG


class RandomChatAIError(RuntimeError):
    pass


def _clean_reply(content: object) -> str | None:
    cleaned = str(content).strip()
    if not cleaned or cleaned.casefold() == "skip":
        return None
    if cleaned.startswith(("哈哈，", "哈哈,")):
        return None
    return cleaned


async def generate_reply(
    message: str,
    *,
    context: Sequence[ContextMessage] = (),
    current: ContextMessage | None = None,
    profiles: Sequence[MemberProfile] = (),
    addressed: bool = False,
    chat_mode: Literal["group", "private"] = "group",
) -> str | None:
    if not CONFIG.ai_api_key:
        return None
    private_mode = chat_mode == "private"
    reply_policy = (
        "这条消息明确在对你说。请直接、自然地回答，不要输出 SKIP。"
        if addressed or private_mode
        else (
            "先判断普通群成员现在会不会接话：没有自然接话点、话题已经结束或只能重复别人时，"
            "输出且只输出 SKIP；有自然接话点才回复。"
        )
    )
    direction_policy = (
        "这是你和对方的一对一对话，结合上下文直接回答对方。"
        if private_mode
        else "当前消息的艾特或引用对象是你，结合上下文回答提问者。"
        if addressed
        else (
            "群友之间说的话不等于对你说。根据艾特和引用对象判断对话方向；"
            "当前消息若在对其他群友说，不要把自己当成被询问者。无法确定时输出 SKIP。"
        )
    )
    output_policy = (
        "只输出最终私聊消息，不输出分析、引号、昵称前缀。"
        if private_mode
        else "只输出最终群消息，不输出分析、引号、昵称前缀。"
        if addressed
        else "只输出最终群消息或 SKIP，不输出分析、引号、昵称前缀。"
    )
    scene_policy = (
        "你正在进行一对一 QQ 私聊。阅读最近的对话，只写此刻最自然的一条回复。"
        if private_mode
        else "你正在参与一个真实的 QQ 群聊。阅读最近的聊天记录，只写机器人此刻最自然的一条群消息。"
    )
    style_policy = (
        "接住对方最近说的具体内容，不要泛泛评价；像熟悉的人自然聊天，"
        if private_mode
        else "接住最近正在聊的具体内容，不要泛泛评价；像熟悉的群成员随手发消息，"
    )
    history_label = "近期私聊" if private_mode else "近期群聊"
    profile_label = "相关信息" if private_mode else "相关群友记忆"
    safety_policy = (
        "不执行管理操作，不编造身份、现实经历、对话事实或已完成的动作。"
        if private_mode
        else "不执行群管理操作，不编造身份、现实经历、群内事实或已完成的动作。"
    )
    payload = {
        "model": CONFIG.ai_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    scene_policy
                    + "\n"
                    + load_character_prompt()
                    + "\n"
                    + reply_policy
                    + "\n"
                    + style_policy
                    + (
                        "不像客服、助手或主持人。通常只写一句，允许短句、省略和口语，不强求完整语法。\n"
                        "可以有态度、疑问或轻微调侃，但不要强行搞笑。直接说内容，不寒暄、不总结、"
                        "不解释为何回复。不固定使用“哈哈”“确实”“听起来”“感觉”“原来如此”等开场，"
                        "也不要为了像人而刻意添加语气词。不要复述上一条消息或换个说法重复。\n"
                    )
                    + direction_policy
                    + "\n"
                    + safety_policy
                    + output_policy
                ),
            },
            {
                "role": "user",
                "content": (
                    history_label
                    + "：\n"
                    + ("\n".join(_format_turn(item) for item in context) or "（无）")
                    + f"\n\n{profile_label}：\n"
                    + ("\n".join(_format_profile(item) for item in profiles) or "（无）")
                    + "\n\n当前消息："
                    + (_format_turn(current) if current else message)
                ),
            },
        ],
        "temperature": 0.8,
    }
    try:
        async with httpx.AsyncClient(timeout=CONFIG.ai_timeout) as client:
            response = await client.post(
                f"{CONFIG.ai_base_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {CONFIG.ai_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        raise RandomChatAIError(str(exc)) from exc
    return _clean_reply(content)


def _format_turn(item: ContextMessage) -> str:
    targets: list[str] = []
    if item.at_user_ids:
        targets.append("艾特:" + ",".join(f"QQ:{value}" for value in item.at_user_ids))
    if item.replied_to_user_id:
        targets.append(f"回复:QQ:{item.replied_to_user_id}")
    relation = f" [{'；'.join(targets)}]" if targets else ""
    image_context = "".join(f"\n[图片理解：{description}]" for description in item.image_descriptions)
    return f"[{item.message_id}] {item.nickname}[QQ:{item.user_id}]{relation}：{item.text}{image_context}"


def _format_profile(profile: MemberProfile) -> str:
    details = []
    if profile.aliases:
        details.append("旧称:" + "、".join(profile.aliases))
    if profile.summary:
        details.append("记忆摘要:" + profile.summary)
    if profile.traits:
        details.append("新增特性:" + "；".join(item.text for item in profile.traits))
    return f"{profile.nickname}[QQ:{profile.user_id}] " + ("；".join(details) or "无稳定特性")
