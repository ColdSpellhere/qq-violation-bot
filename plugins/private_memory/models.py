from __future__ import annotations

from dataclasses import dataclass


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
class RelationshipState:
    id: int
    scope: ConversationScope
    state_text: str
    open_topics_json: str
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


@dataclass(frozen=True)
class MigrationReport:
    schema_version: int
    tables_created: int
    columns_added: int
