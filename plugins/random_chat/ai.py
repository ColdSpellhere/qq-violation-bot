from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
import logging
from typing import TYPE_CHECKING, Literal, Protocol

import httpx

from plugins.chat_archive.db import ContextMessage
from plugins.chat_prompt import ChatPromptInput, build_chat_prompt
from plugins.feature_control.runtime import FEATURES
from plugins.llm_gateway import get_gateway
from plugins.llm_gateway.errors import GatewayError
from plugins.member_memory.store import MemberProfile
from plugins.random_chat.persona import load_character_prompt
from plugins.violation_record.config import CONFIG

if TYPE_CHECKING:
    from plugins.chat_vision.client import VisionImage


logger = logging.getLogger(__name__)


class RandomChatAIError(RuntimeError):
    pass


class RelationshipStateLike(Protocol):
    state_text: str
    open_topics: tuple[str, ...]
    preferred_address: str
    communication_style: str


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
    images: Sequence[VisionImage] = (),
    relationship: RelationshipStateLike | None = None,
    open_topics: tuple[str, ...] = (),
    legacy_profiles: Sequence[MemberProfile] | None = None,
) -> str | None:
    if not CONFIG.ai_api_key:
        return None
    feature_state = FEATURES.snapshot()
    if not feature_state.relationship_state_enabled:
        relationship = None
        open_topics = ()
    effective_profiles = tuple(profiles)
    if chat_mode == "private" and current is not None and current.user_id:
        effective_profiles = tuple(
            profile
            for profile in effective_profiles
            if str(profile.user_id) == str(current.user_id)
        )
        scope = getattr(relationship, "scope", None)
        if scope is not None and str(getattr(scope, "user_id", "")) != str(
            current.user_id
        ):
            relationship = None
            open_topics = ()
    effective_legacy_profiles = (
        tuple(legacy_profiles) if legacy_profiles is not None else effective_profiles
    )
    if chat_mode == "private" and current is not None and current.user_id:
        effective_legacy_profiles = tuple(
            profile
            for profile in effective_legacy_profiles
            if str(profile.user_id) == str(current.user_id)
        )
    if not feature_state.relationship_state_enabled:
        effective_legacy_profiles = effective_profiles
    persona = load_character_prompt()
    legacy_messages = _legacy_messages(
        message,
        context=context,
        current=current,
        profiles=effective_legacy_profiles,
        addressed=addressed,
        chat_mode=chat_mode,
        persona=persona,
    )
    messages = legacy_messages
    if feature_state.prompt_builder_enabled:
        try:
            current_message = current or ContextMessage(
                "当前用户", message, message_id="current", user_id=""
            )
            descriptions = tuple(
                description
                for item in (*context, current_message)
                for description in item.image_descriptions
                if description.strip()
            )
            messages = build_chat_prompt(
                ChatPromptInput(
                    mode=chat_mode,
                    now_text=datetime.now().astimezone().isoformat(timespec="minutes"),
                    persona=persona,
                    context=tuple(context),
                    profiles=effective_profiles,
                    relationship=relationship,
                    open_topics=tuple(open_topics),
                    image_descriptions=descriptions,
                    current=current_message,
                    addressed=addressed,
                )
            ).messages
        except Exception as exc:
            logger.warning(
                "chat prompt builder failed error_class=%s", type(exc).__name__
            )
            messages = legacy_messages

    messages = _attach_images(messages, images)
    try:
        if FEATURES.llm_gateway_allowed("chat"):
            gateway = await get_gateway()
            content = await gateway.generate_chat_reply(messages, images=bool(images))
        else:
            content = await _legacy_complete(messages, images=bool(images))
    except GatewayError as exc:
        raise RandomChatAIError(type(exc).__name__) from None
    except Exception as exc:
        raise RandomChatAIError(str(exc)) from exc
    return _clean_reply(content)


def _legacy_messages(
    message: str,
    *,
    context: Sequence[ContextMessage],
    current: ContextMessage | None,
    profiles: Sequence[MemberProfile],
    addressed: bool,
    chat_mode: Literal["group", "private"],
    persona: str,
) -> tuple[dict[str, object], ...]:
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
    user_prompt = (
        history_label
        + "：\n"
        + ("\n".join(_format_turn(item) for item in context) or "（无）")
        + f"\n\n{profile_label}：\n"
        + ("\n".join(_format_profile(item) for item in profiles) or "（无）")
        + "\n\n当前消息："
        + (_format_turn(current) if current else message)
    )
    return (
        {
            "role": "system",
            "content": (
                scene_policy
                + "\n"
                + persona
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
            "content": user_prompt,
        },
    )


def _attach_images(
    messages: Sequence[dict[str, object]], images: Sequence[VisionImage]
) -> tuple[dict[str, object], ...]:
    copied = tuple(dict(item) for item in messages)
    if not images:
        return copied
    from plugins.chat_vision.client import image_data_url

    user = dict(copied[-1])
    text = user.get("content", "")
    user["content"] = [
        {"type": "text", "text": str(text)},
        *(
            {
                "type": "image_url",
                "image_url": {"url": image_data_url(image.content, image.mime_type)},
            }
            for image in images
        ),
    ]
    return (*copied[:-1], user)


async def _legacy_complete(
    messages: Sequence[dict[str, object]], *, images: bool
) -> object:
    payload: dict[str, object] = {
        "model": CONFIG.chat_vision_model if images else CONFIG.ai_model,
        "messages": list(messages),
    }
    if images:
        payload["thinking"] = {"type": "disabled"}
    else:
        payload["temperature"] = 0.8
    request_timeout = CONFIG.chat_vision_timeout if images else CONFIG.ai_timeout
    async with httpx.AsyncClient(timeout=request_timeout) as client:
        response = await client.post(
            f"{CONFIG.ai_base_url}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {CONFIG.ai_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


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
