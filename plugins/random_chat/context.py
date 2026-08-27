from __future__ import annotations

from collections import Counter
import re
import unicodedata
from typing import Sequence

from plugins.chat_archive.db import ContextMessage


_MENTION_RE = re.compile(r"@[^\s@,，、。.!！?？;；:：]+")
_REPEAT_KEY_RE = re.compile(r"[\W_]+", re.UNICODE)
_LONG_REPEAT_MIN_CHARS = 28
_MAX_CONTEXT_CANDIDATES = 120


def context_candidate_limit(final_limit: int) -> int:
    """Return a bounded candidate window large enough to survive filtering."""
    if final_limit <= 0:
        return 0
    return min(
        _MAX_CONTEXT_CANDIDATES,
        max(final_limit * 2, final_limit + 10),
    )


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _repeat_key(value: str) -> str:
    return _REPEAT_KEY_RE.sub("", _normalized_text(value))


def _mention_spam_signature(item: ContextMessage) -> str | None:
    if item.reply_message_id or item.image_descriptions:
        return None
    text = _normalized_text(item.text)
    mentions = _MENTION_RE.findall(text)
    mention_counts = Counter(mentions)
    at_counts = Counter(str(value) for value in item.at_user_ids if value)
    repeated_mentions = sorted(
        mention for mention, count in mention_counts.items() if count >= 3
    )
    repeated_targets = sorted(
        target for target, count in at_counts.items() if count >= 3
    )
    if not repeated_mentions and not repeated_targets:
        return None
    collapsed = text
    for mention in repeated_mentions:
        collapsed = re.sub(
            rf"(?:{re.escape(mention)}(?:[\s,，、]*)){{2,}}",
            mention + " ",
            collapsed,
        )
    return "|".join(
        (
            ",".join(repeated_targets),
            _repeat_key(collapsed),
        )
    )


def _long_repeat_signature(item: ContextMessage) -> tuple[str, str] | None:
    if item.reply_message_id or item.image_descriptions:
        return None
    key = _repeat_key(item.text)
    if len(key) < _LONG_REPEAT_MIN_CHARS:
        return None
    return str(item.user_id), key


def _deduplicate_noise(
    messages: Sequence[ContextMessage],
    *,
    quoted_message_id: str | None,
) -> list[ContextMessage]:
    kept_reversed: list[ContextMessage] = []
    seen_mention_spam: set[str] = set()
    seen_long_repeats: set[tuple[str, str]] = set()
    for item in reversed(messages):
        protected = bool(
            quoted_message_id and item.message_id == quoted_message_id
        )
        mention_signature = _mention_spam_signature(item)
        long_signature = _long_repeat_signature(item)
        if not protected:
            if (
                mention_signature is not None
                and mention_signature in seen_mention_spam
            ):
                continue
            if long_signature is not None and long_signature in seen_long_repeats:
                continue
        if mention_signature is not None:
            seen_mention_spam.add(mention_signature)
        if long_signature is not None:
            seen_long_repeats.add(long_signature)
        kept_reversed.append(item)
    return list(reversed(kept_reversed))


def _limit_self_history(
    messages: Sequence[ContextMessage],
    *,
    max_self_messages: int,
    quoted_message_id: str | None,
) -> list[ContextMessage]:
    kept_reversed: list[ContextMessage] = []
    self_count = 0
    for item in reversed(messages):
        quoted = bool(quoted_message_id and item.message_id == quoted_message_id)
        if item.is_bot and not quoted:
            if self_count >= max(0, max_self_messages):
                continue
            self_count += 1
        kept_reversed.append(item)
    return list(reversed(kept_reversed))


def _priority_indices(
    messages: Sequence[ContextMessage],
    *,
    quoted_message_id: str | None,
    current_user_id: str | None,
) -> list[tuple[int, ...]]:
    priorities: list[tuple[int, ...]] = []
    if quoted_message_id:
        quoted = tuple(
            index
            for index, item in enumerate(messages)
            if item.message_id == quoted_message_id
        )
        if quoted:
            priorities.append(quoted)
    if current_user_id:
        for index in range(len(messages) - 1, -1, -1):
            if str(messages[index].user_id) == str(current_user_id):
                priorities.append((index,))
                break
    message_indices = {
        item.message_id: index for index, item in enumerate(messages)
    }
    for bot_index in range(len(messages) - 1, -1, -1):
        item = messages[bot_index]
        if not item.is_bot:
            continue
        trigger_index = message_indices.get(item.reply_message_id or "")
        priorities.append(
            (bot_index, trigger_index)
            if trigger_index is not None
            else (bot_index,)
        )
    return priorities


def select_chat_context(
    messages: Sequence[ContextMessage],
    *,
    limit: int,
    max_self_messages: int = 3,
    quoted_message_id: str | None = None,
    current_user_id: str | None = None,
) -> list[ContextMessage]:
    """Select a compact chronological context without losing causal anchors."""
    if limit <= 0 or not messages:
        return []
    deduplicated = _deduplicate_noise(
        messages,
        quoted_message_id=quoted_message_id,
    )
    filtered = _limit_self_history(
        deduplicated,
        max_self_messages=max_self_messages,
        quoted_message_id=quoted_message_id,
    )
    if len(filtered) <= limit:
        return filtered
    priorities = _priority_indices(
        filtered,
        quoted_message_id=quoted_message_id,
        current_user_id=current_user_id,
    )
    kept: set[int] = set()
    for group in priorities:
        additions = {index for index in group if index not in kept}
        if len(kept) + len(additions) <= limit:
            kept.update(additions)
    for index in range(len(filtered) - 1, -1, -1):
        if len(kept) >= limit:
            break
        kept.add(index)
    return [item for index, item in enumerate(filtered) if index in kept]


__all__ = ["context_candidate_limit", "select_chat_context"]
