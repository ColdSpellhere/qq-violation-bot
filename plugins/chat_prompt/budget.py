from __future__ import annotations

import json
from dataclasses import replace

from .models import (
    BudgetedPromptData,
    ChatPromptInput,
    ContextMessageLike,
    MemberProfileLike,
    PromptBudget,
    RelationshipStateLike,
    TruncationCounters,
)
from .speakers import build_speaker_directory
from .sanitize import neutralize_role_markers


def _clip(value: str, limit: int) -> tuple[str, int]:
    if len(value) <= limit:
        return value, 0
    return value[:limit], len(value) - limit


def _context_text(item: ContextMessageLike, speaker_ref: str) -> str:
    return json.dumps(
        {
            "speaker_ref": speaker_ref,
            "message_id": item.message_id,
            "sender_qq": item.user_id,
            "nickname": item.nickname,
            "at_targets": item.at_user_ids,
            "reply_message_id": item.reply_message_id,
            "reply_author_qq": item.replied_to_user_id,
            "speaker_role": (
                "assistant_history"
                if getattr(item, "is_bot", False)
                else "peer_bot"
                if getattr(item, "is_peer_bot", False)
                else "group_member"
            ),
            "text": neutralize_role_markers(item.text),
            "images": item.image_descriptions,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _profile_text(profile: MemberProfileLike, speaker_ref: str) -> str:
    details: list[str] = []
    if profile.summary.strip():
        details.append(profile.summary.strip())
    details.extend(
        trait.text.strip() for trait in profile.traits if trait.text.strip()
    )
    return json.dumps(
        {
            "speaker_ref": speaker_ref,
            "qq": profile.user_id,
            "nickname": profile.nickname,
            "memory": "；".join(details),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _prune_speakers(data: BudgetedPromptData) -> BudgetedPromptData:
    used = {
        data.current_speaker_ref,
        *data.context_speaker_refs,
        *data.fact_speaker_refs,
        *data.current_at_speaker_refs,
    }
    if data.current_reply_author_ref:
        used.add(data.current_reply_author_ref)
    return replace(
        data,
        speakers=tuple(item for item in data.speakers if item.ref in used),
    )


def _relationship_text(value: RelationshipStateLike | None) -> str:
    if value is None:
        return ""
    return json.dumps(
        {
            "state": value.state_text,
            "preferred_address": value.preferred_address,
            "communication_style": value.communication_style,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _fit_sequence(
    values: tuple[str, ...], limit: int
) -> tuple[tuple[str, ...], int, int]:
    kept: list[str] = []
    remaining = limit
    removed_items = 0
    removed_chars = 0
    for index, value in enumerate(values):
        if remaining <= 0:
            removed_items += len(values) - index
            removed_chars += sum(len(item) for item in values[index:])
            break
        if len(value) <= remaining:
            kept.append(value)
            remaining -= len(value)
            continue
        kept.append(value[:remaining])
        removed_chars += len(value) - remaining
        removed_items += len(values) - index - 1
        removed_chars += sum(len(item) for item in values[index + 1 :])
        break
    return tuple(kept), removed_items, removed_chars


def prompt_data_chars(data: BudgetedPromptData) -> int:
    return sum(
        (
            len(data.persona),
            sum(map(len, data.context)),
            sum(map(len, data.facts)),
            len(data.relationship),
            sum(map(len, data.open_topics)),
            sum(map(len, data.image_descriptions)),
            sum(map(len, data.web_search_data)),
            len(data.current),
        )
    )


def _trim_tail(values: tuple[str, ...], excess: int) -> tuple[tuple[str, ...], int, int]:
    if excess <= 0 or not values:
        return values, 0, 0
    target = max(0, sum(map(len, values)) - excess)
    return _fit_sequence(values, target)


def apply_prompt_budget(
    source: ChatPromptInput, budget: PromptBudget = PromptBudget()
) -> BudgetedPromptData:
    persona, persona_removed = _clip(source.persona, budget.persona_chars)
    safe_current = neutralize_role_markers(source.current.text)
    current, current_removed = _clip(safe_current, budget.current_chars)

    directory = build_speaker_directory(
        current=source.current,
        context=source.context,
        profiles=source.profiles,
    )
    current_ref = (
        directory.ref_for_user(source.current.user_id)
        or directory.ref_for_message(source.current.message_id)
    )
    if current_ref is None:
        raise ValueError("current speaker must have a stable request-local reference")
    raw_context_refs = tuple(
        directory.ref_for_user(item.user_id)
        or directory.ref_for_message(item.message_id)
        or ""
        for item in source.context
    )
    raw_context = tuple(
        _context_text(item, speaker_ref)
        for item, speaker_ref in zip(source.context, raw_context_refs)
    )
    raw_ids = tuple(item.message_id for item in source.context)
    count_removed = max(0, len(raw_context) - budget.context_messages)
    context = raw_context[count_removed:]
    context_ids = raw_ids[count_removed:]
    context_refs = raw_context_refs[count_removed:]
    while len(context) > 1 and sum(map(len, context)) > budget.context_chars:
        count_removed += 1
        context = context[1:]
        context_ids = context_ids[1:]
        context_refs = context_refs[1:]
    context_original_chars = sum(map(len, raw_context))
    if context and sum(map(len, context)) > budget.context_chars:
        context = (context[-1][: budget.context_chars],)
        context_ids = (context_ids[-1],)
        context_refs = (context_refs[-1],)
    context_chars_removed = context_original_chars - sum(map(len, context))

    raw_fact_refs = tuple(
        directory.ref_for_user(item.user_id) or "" for item in source.profiles
    )
    raw_facts = tuple(
        _profile_text(item, speaker_ref)
        for item, speaker_ref in zip(source.profiles, raw_fact_refs)
    )
    facts, _, facts_removed = _fit_sequence(raw_facts, budget.facts_chars)
    fact_refs = raw_fact_refs[: len(facts)]
    raw_relationship = _relationship_text(source.relationship)
    relationship, relationship_removed = _clip(
        raw_relationship, budget.relationship_chars
    )
    raw_topics = source.open_topics[:5]
    topics_count_removed = max(0, len(source.open_topics) - len(raw_topics))
    topics, extra_topics_removed, topics_chars_removed = _fit_sequence(
        raw_topics, budget.topics_chars
    )
    topics_count_removed += extra_topics_removed
    topics_chars_removed += sum(map(len, source.open_topics[5:]))
    raw_images = source.image_descriptions
    images, images_count_removed, images_chars_removed = _fit_sequence(
        raw_images, budget.images_chars
    )
    searches, _, search_chars_removed = _fit_sequence(
        source.web_search_data, budget.web_search_chars
    )

    data = BudgetedPromptData(
        mode=source.mode,
        now_text=source.now_text,
        persona=persona,
        speakers=directory.identities,
        context=context,
        context_message_ids=context_ids,
        context_speaker_refs=context_refs,
        facts=facts,
        fact_speaker_refs=fact_refs,
        relationship=relationship,
        open_topics=topics,
        image_descriptions=images,
        web_search_data=searches,
        web_search_failed=source.web_search_failed,
        current=current,
        current_text=current,
        current_message_id=source.current.message_id,
        current_user_id=source.current.user_id,
        current_nickname=source.current.nickname,
        current_at_user_ids=source.current.at_user_ids,
        current_reply_message_id=source.current.reply_message_id,
        current_replied_to_user_id=source.current.replied_to_user_id,
        current_speaker_ref=current_ref,
        current_at_speaker_refs=tuple(
            ref
            for user_id in source.current.at_user_ids
            if (ref := directory.ref_for_user(user_id)) is not None
        ),
        current_reply_author_ref=directory.ref_for_user(
            source.current.replied_to_user_id or ""
        ),
        addressed=source.addressed,
        required_reply=source.required_reply,
        truncation=TruncationCounters(
            persona_chars_removed=persona_removed,
            context_messages_removed=count_removed,
            context_chars_removed=context_chars_removed,
            facts_chars_removed=facts_removed,
            relationship_chars_removed=relationship_removed,
            topics_removed=topics_count_removed,
            topics_chars_removed=topics_chars_removed,
            images_removed=images_count_removed,
            images_chars_removed=images_chars_removed,
            web_search_chars_removed=search_chars_removed,
            current_chars_removed=current_removed,
        ),
    )

    for field, counter_chars, counter_items in (
        ("web_search_data", "web_search_chars_removed", None),
        ("image_descriptions", "images_chars_removed", "images_removed"),
        ("open_topics", "topics_chars_removed", "topics_removed"),
        ("facts", "facts_chars_removed", None),
    ):
        excess = prompt_data_chars(data) - budget.total_chars
        if excess <= 0:
            break
        values = getattr(data, field)
        trimmed, removed_items, removed_chars = _trim_tail(values, excess)
        updates = {
            counter_chars: getattr(data.truncation, counter_chars) + removed_chars
        }
        if counter_items is not None:
            updates[counter_items] = (
                getattr(data.truncation, counter_items) + removed_items
            )
        data = replace(
            data,
            **{field: trimmed},
            truncation=replace(data.truncation, **updates),
        )
        if field == "facts":
            data = replace(data, fact_speaker_refs=data.fact_speaker_refs[: len(trimmed)])
        data = _prune_speakers(data)

    while len(data.context) > 2 and prompt_data_chars(data) > budget.total_chars:
        removed = data.context[0]
        counters = replace(
            data.truncation,
            context_messages_removed=data.truncation.context_messages_removed + 1,
            context_chars_removed=data.truncation.context_chars_removed + len(removed),
        )
        data = replace(
            data,
            context=data.context[1:],
            context_message_ids=data.context_message_ids[1:],
            context_speaker_refs=data.context_speaker_refs[1:],
            truncation=counters,
        )
        data = _prune_speakers(data)

    for field, counter in (
        ("relationship", "relationship_chars_removed"),
        ("persona", "persona_chars_removed"),
    ):
        excess = prompt_data_chars(data) - budget.total_chars
        if excess <= 0:
            break
        value = getattr(data, field)
        trimmed = value[: max(0, len(value) - excess)]
        removed = len(value) - len(trimmed)
        data = replace(
            data,
            **{field: trimmed},
            truncation=replace(
                data.truncation,
                **{counter: getattr(data.truncation, counter) + removed},
            ),
        )

    while data.context and prompt_data_chars(data) > budget.total_chars:
        removed = data.context[0]
        counters = replace(
            data.truncation,
            context_messages_removed=data.truncation.context_messages_removed + 1,
            context_chars_removed=data.truncation.context_chars_removed + len(removed),
        )
        data = replace(
            data,
            context=data.context[1:],
            context_message_ids=data.context_message_ids[1:],
            context_speaker_refs=data.context_speaker_refs[1:],
            truncation=counters,
        )
        data = _prune_speakers(data)

    excess = prompt_data_chars(data) - budget.total_chars
    if excess > 0:
        target = max(1, len(data.current) - excess)
        trimmed = data.current[:target]
        removed = len(data.current) - len(trimmed)
        data = replace(
            data,
            current=trimmed,
            current_text=trimmed,
            truncation=replace(
                data.truncation,
                current_chars_removed=data.truncation.current_chars_removed + removed,
            ),
        )
    return _prune_speakers(data)


__all__ = ["apply_prompt_budget", "prompt_data_chars"]
