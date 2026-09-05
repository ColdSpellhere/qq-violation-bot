from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


_PERSONA_ID_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", re.ASCII
)


def validate_persona_id(persona_id: str) -> str:
    if not isinstance(persona_id, str) or _PERSONA_ID_RE.fullmatch(persona_id) is None:
        raise ValueError(
            "persona_id must be a lowercase ASCII slug of at most 64 characters"
        )
    return persona_id


@dataclass(frozen=True)
class ConversationScope:
    conversation_kind: str
    user_id: str
    group_id: int | None = None
    persona_id: str = "radish-cat"


@dataclass(frozen=True)
class PrivateMessage:
    id: int
    user_id: str
    message_id: str
    direction: str
    text: str
    content_hash: str
    event_time: int
    created_at: str
    expires_at: str
    purged_at: str | None = None
    source_kind: str = "text"
    source_message_id: str | None = None


@dataclass(frozen=True)
class PrivateSummary:
    user_id: str
    summary_text: str
    source_start_id: int
    source_end_id: int
    summarized_through_id: int
    version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PrivateFact:
    id: int
    user_id: str
    fact_text: str
    source_message_id: str
    source_quote: str
    trust_level: str
    status: str
    supersedes_id: int | None
    version: int
    created_at: str
    updated_at: str
    deleted_at: str | None = None


@dataclass(frozen=True)
class PrivateFactCandidate:
    user_id: str
    fact_text: str
    source_message_id: str
    source_quote: str


@dataclass(frozen=True)
class PurgeReport:
    purged_messages: int
    checkpoint_complete: bool = True


@dataclass(frozen=True)
class ClearReport:
    purged_messages: int
    summaries_deleted: int
    topics_cleared: int
    jobs_cancelled: int
    checkpoint_complete: bool = True


@dataclass(frozen=True)
class RelationshipState:
    id: int
    scope: ConversationScope
    state_text: str
    open_topics: tuple[str, ...]
    preferred_address: str
    communication_style: str
    source_message_id: str
    source_watermark: int
    version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class MemoryJob:
    id: int
    job_type: str
    scope: ConversationScope
    input_through_id: int
    expected_version: int
    status: str
    attempts: int
    next_run_at: str
    lease_owner: str | None
    lease_expires_at: str | None
    claim_version: int
    error_code: str
    error_summary: str
    created_at: str
    updated_at: str
    input_from_id: int = 0


class MemoryJobContinuation(Enum):
    MORE = "more"


@dataclass(frozen=True)
class MigrationReport:
    schema_version: int
    tables_created: int
    columns_added: int
