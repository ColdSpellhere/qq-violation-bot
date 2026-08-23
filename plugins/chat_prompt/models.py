from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


class ContextMessageLike(Protocol):
    nickname: str
    text: str
    message_id: str
    user_id: str
    at_user_ids: tuple[str, ...]
    reply_message_id: str | None
    replied_to_user_id: str | None
    image_descriptions: tuple[str, ...]


class MemberProfileLike(Protocol):
    user_id: str
    nickname: str
    summary: str
    traits: tuple[object, ...]


class RelationshipStateLike(Protocol):
    state_text: str
    preferred_address: str
    communication_style: str


@dataclass(frozen=True)
class SpeakerIdentity:
    ref: str
    user_id: str
    nickname: str
    current: bool = False


@dataclass(frozen=True)
class SpeakerDirectory:
    identities: tuple[SpeakerIdentity, ...]
    refs_by_user: tuple[tuple[str, str], ...]
    refs_by_message: tuple[tuple[str, str], ...]

    def ref_for_user(self, user_id: str) -> str | None:
        normalized = str(user_id).strip()
        return next(
            (ref for value, ref in self.refs_by_user if value == normalized), None
        )

    def ref_for_message(self, message_id: str) -> str | None:
        normalized = str(message_id).strip()
        return next(
            (ref for value, ref in self.refs_by_message if value == normalized), None
        )


def _has_fields(value: object, fields: tuple[str, ...]) -> bool:
    return all(hasattr(value, field) for field in fields)


@dataclass(frozen=True)
class ChatPromptInput:
    mode: Literal["group", "private"]
    now_text: str
    persona: str
    context: tuple[ContextMessageLike, ...]
    profiles: tuple[MemberProfileLike, ...]
    relationship: RelationshipStateLike | None
    open_topics: tuple[str, ...]
    image_descriptions: tuple[str, ...]
    current: ContextMessageLike
    addressed: bool

    def __post_init__(self) -> None:
        if self.mode not in {"group", "private"}:
            raise ValueError("mode must be group or private")
        if type(self.now_text) is not str or not self.now_text.strip():
            raise ValueError("now_text must be non-empty text")
        if type(self.persona) is not str:
            raise ValueError("persona must be text")
        context_fields = (
            "nickname",
            "text",
            "message_id",
            "user_id",
            "at_user_ids",
            "reply_message_id",
            "replied_to_user_id",
            "image_descriptions",
        )
        if not _has_fields(self.current, context_fields):
            raise ValueError("current must be ContextMessage")
        if type(self.context) is not tuple or not all(
            _has_fields(item, context_fields) for item in self.context
        ):
            raise ValueError("context must be a typed tuple")
        profile_fields = ("user_id", "nickname", "summary", "traits")
        if type(self.profiles) is not tuple or not all(
            _has_fields(item, profile_fields) for item in self.profiles
        ):
            raise ValueError("profiles must be a typed tuple")
        for value, name in (
            (self.open_topics, "open_topics"),
            (self.image_descriptions, "image_descriptions"),
        ):
            if type(value) is not tuple or not all(type(item) is str for item in value):
                raise ValueError(f"{name} must be a text tuple")
        if self.relationship is not None and not _has_fields(
            self.relationship,
            ("state_text", "preferred_address", "communication_style"),
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


@dataclass(frozen=True)
class RenderedPrompt:
    messages: tuple[dict[str, object], ...]
    total_chars: int
    truncation: TruncationCounters


__all__ = [
    "BudgetedPromptData",
    "ChatPromptInput",
    "ContextMessageLike",
    "MemberProfileLike",
    "PromptBudget",
    "RelationshipStateLike",
    "RenderedPrompt",
    "SpeakerDirectory",
    "SpeakerIdentity",
    "TruncationCounters",
]
