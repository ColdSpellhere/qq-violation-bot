from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from plugins.chat_archive.db import ContextMessage
from plugins.member_memory.store import MemberProfile
from plugins.private_memory.models import RelationshipState


@dataclass(frozen=True)
class ChatPromptInput:
    mode: Literal["group", "private"]
    now_text: str
    persona: str
    context: tuple[ContextMessage, ...]
    profiles: tuple[MemberProfile, ...]
    relationship: RelationshipState | None
    open_topics: tuple[str, ...]
    image_descriptions: tuple[str, ...]
    current: ContextMessage
    addressed: bool

    def __post_init__(self) -> None:
        if self.mode not in {"group", "private"}:
            raise ValueError("mode must be group or private")
        if type(self.now_text) is not str or not self.now_text.strip():
            raise ValueError("now_text must be non-empty text")
        if type(self.persona) is not str:
            raise ValueError("persona must be text")
        if not isinstance(self.current, ContextMessage):
            raise ValueError("current must be ContextMessage")
        for value, item_type, name in (
            (self.context, ContextMessage, "context"),
            (self.profiles, MemberProfile, "profiles"),
        ):
            if type(value) is not tuple or not all(
                isinstance(item, item_type) for item in value
            ):
                raise ValueError(f"{name} must be a typed tuple")
        for value, name in (
            (self.open_topics, "open_topics"),
            (self.image_descriptions, "image_descriptions"),
        ):
            if type(value) is not tuple or not all(type(item) is str for item in value):
                raise ValueError(f"{name} must be a text tuple")
        if self.relationship is not None and not isinstance(
            self.relationship, RelationshipState
        ):
            raise ValueError("relationship must be RelationshipState or None")
        if type(self.addressed) is not bool:
            raise ValueError("addressed must be bool")


@dataclass(frozen=True)
class PromptBudget:
    persona_chars: int = 2000
    context_messages: int = 20
    context_chars: int = 6000
    facts_chars: int = 1200
    relationship_chars: int = 600
    topics_chars: int = 400
    images_chars: int = 2000
    current_chars: int = 2000
    total_chars: int = 12000

    def __post_init__(self) -> None:
        for value in self.__dict__.values():
            if type(value) is not int or value <= 0:
                raise ValueError("prompt budgets must be positive integers")


@dataclass(frozen=True)
class TruncationCounters:
    persona_chars_removed: int = 0
    context_messages_removed: int = 0
    context_chars_removed: int = 0
    facts_chars_removed: int = 0
    relationship_chars_removed: int = 0
    topics_removed: int = 0
    topics_chars_removed: int = 0
    images_removed: int = 0
    images_chars_removed: int = 0
    current_chars_removed: int = 0


@dataclass(frozen=True)
class BudgetedPromptData:
    mode: Literal["group", "private"]
    now_text: str
    persona: str
    context: tuple[str, ...]
    context_message_ids: tuple[str, ...]
    facts: tuple[str, ...]
    relationship: str
    open_topics: tuple[str, ...]
    image_descriptions: tuple[str, ...]
    current: str
    current_text: str
    current_message_id: str
    current_user_id: str
    current_nickname: str
    current_at_user_ids: tuple[str, ...]
    current_reply_message_id: str | None
    current_replied_to_user_id: str | None
    addressed: bool
    truncation: TruncationCounters
    safety_required: bool = True
    direction_required: bool = True
    output_contract_required: bool = True


__all__ = [
    "BudgetedPromptData",
    "ChatPromptInput",
    "PromptBudget",
    "TruncationCounters",
]
