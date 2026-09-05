-- Synthetic fixture: exact schema definitions at Carrot a37b1a6; no production data.

CREATE TABLE IF NOT EXISTS chat_messages (
    message_id TEXT PRIMARY KEY,
    group_id INTEGER NOT NULL,
    event_time INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    sender_json TEXT NOT NULL,
    message_json TEXT NOT NULL,
    plaintext TEXT NOT NULL,
    reply_message_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_time
ON chat_messages(event_time, message_id);


    CREATE TABLE IF NOT EXISTS private_chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        direction TEXT NOT NULL CHECK(direction IN ('user','assistant')),
        text TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        event_time INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        purged_at TEXT,
        source_kind TEXT NOT NULL,
        source_message_id TEXT,
        image_descriptions_json TEXT NOT NULL DEFAULT '[]',
        UNIQUE(user_id,direction,message_id)
    )
    ;

    CREATE TABLE IF NOT EXISTS private_conversation_summaries (
        user_id TEXT PRIMARY KEY,
        summary_text TEXT NOT NULL,
        source_start_id INTEGER NOT NULL,
        source_end_id INTEGER NOT NULL,
        summarized_through_id INTEGER NOT NULL,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(source_start_id >= 0),
        CHECK(source_end_id >= source_start_id),
        CHECK(summarized_through_id >= source_end_id)
    )
    ;

    CREATE TABLE IF NOT EXISTS private_memory_facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        fact_text TEXT NOT NULL,
        normalized_text TEXT NOT NULL,
        source_message_id TEXT NOT NULL,
        source_quote TEXT NOT NULL DEFAULT '',
        trust_level TEXT NOT NULL DEFAULT 'ai_extracted'
            CHECK(trust_level IN ('ai_extracted','admin_confirmed')),
        status TEXT NOT NULL DEFAULT 'active'
            CHECK(status IN ('active','superseded','deleted')),
        supersedes_id INTEGER,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        deleted_at TEXT,
        UNIQUE(user_id,normalized_text,source_message_id),
        FOREIGN KEY(supersedes_id) REFERENCES private_memory_facts(id)
    )
    ;

    CREATE TABLE IF NOT EXISTS relationship_states (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_kind TEXT NOT NULL
            CHECK(conversation_kind IN ('group','private')),
        group_id INTEGER,
        user_id TEXT NOT NULL,
        persona_id TEXT NOT NULL,
        state_text TEXT NOT NULL DEFAULT '',
        open_topics_json TEXT NOT NULL DEFAULT '[]',
        preferred_address TEXT NOT NULL DEFAULT '',
        communication_style TEXT NOT NULL DEFAULT '',
        source_message_id TEXT NOT NULL DEFAULT '',
        source_watermark INTEGER NOT NULL DEFAULT 0 CHECK(source_watermark >= 0),
        version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(
            (conversation_kind='group' AND group_id IS NOT NULL)
            OR (conversation_kind='private' AND group_id IS NULL)
        )
    )
    ;

    CREATE TABLE IF NOT EXISTS memory_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_type TEXT NOT NULL
            CHECK(job_type IN ('private_summary','private_facts','relationship')),
        conversation_kind TEXT NOT NULL
            CHECK(conversation_kind IN ('group','private')),
        group_id INTEGER,
        user_id TEXT NOT NULL,
        persona_id TEXT NOT NULL DEFAULT 'radish-cat',
        input_through_id INTEGER NOT NULL CHECK(input_through_id >= 0),
        expected_version INTEGER NOT NULL CHECK(expected_version >= 0),
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','running','succeeded','failed','cancelled')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
        next_run_at TEXT NOT NULL,
        lease_owner TEXT,
        lease_expires_at TEXT,
        claim_version INTEGER NOT NULL DEFAULT 0 CHECK(claim_version >= 0),
        error_code TEXT NOT NULL DEFAULT '',
        error_summary TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(
            (conversation_kind='group' AND group_id IS NOT NULL)
            OR (conversation_kind='private' AND group_id IS NULL)
        )
    )
    ;

    CREATE TABLE IF NOT EXISTS memory_pending_operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        confirmation_token_hash TEXT NOT NULL UNIQUE CHECK(
            length(confirmation_token_hash)=64
            AND confirmation_token_hash NOT GLOB '*[^0-9a-f]*'
        ),
        operator_user_id TEXT NOT NULL,
        operation_type TEXT NOT NULL,
        target_kind TEXT NOT NULL CHECK(target_kind IN ('group','private','fact','relationship')),
        target_group_id INTEGER,
        target_user_id TEXT NOT NULL,
        target_memory_id INTEGER,
        payload_json TEXT NOT NULL,
        preview_text TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed_at TEXT,
        created_at TEXT NOT NULL
    )
    ;

    CREATE TABLE IF NOT EXISTS memory_governance_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id INTEGER,
        operator_user_id TEXT NOT NULL,
        target_kind TEXT NOT NULL,
        target_group_id INTEGER,
        target_user_id TEXT NOT NULL,
        target_memory_id INTEGER,
        operation_type TEXT NOT NULL,
        before_hash TEXT NOT NULL DEFAULT '',
        after_hash TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL,
        result TEXT NOT NULL CHECK(result IN ('success','failed','cancelled')),
        error_code TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )
    ;

    CREATE TABLE IF NOT EXISTS llm_usage_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task TEXT NOT NULL,
        model TEXT NOT NULL,
        input_tokens INTEGER CHECK(input_tokens IS NULL OR input_tokens >= 0),
        output_tokens INTEGER CHECK(output_tokens IS NULL OR output_tokens >= 0),
        total_tokens INTEGER CHECK(total_tokens IS NULL OR total_tokens >= 0),
        cost_microunits INTEGER CHECK(cost_microunits IS NULL OR cost_microunits >= 0),
        cost_currency TEXT,
        latency_ms INTEGER NOT NULL CHECK(latency_ms >= 0),
        status TEXT NOT NULL CHECK(status IN ('success','failure')),
        retry_count INTEGER NOT NULL CHECK(retry_count >= 0),
        error_class TEXT,
        created_at TEXT NOT NULL
    )
    ;

    CREATE TABLE IF NOT EXISTS private_memory_schema_meta (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        schema_version INTEGER NOT NULL CHECK(schema_version > 0),
        updated_at TEXT NOT NULL
    )
    ;
CREATE INDEX IF NOT EXISTS idx_private_chat_messages_user_id ON private_chat_messages(user_id,id);
CREATE INDEX IF NOT EXISTS idx_private_chat_messages_expiry ON private_chat_messages(expires_at,id) WHERE purged_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_private_memory_facts_active ON private_memory_facts(user_id,id) WHERE status='active';
CREATE INDEX IF NOT EXISTS idx_relationship_states_scope ON relationship_states(conversation_kind,group_id,user_id,persona_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_relationship_states_group_unique ON relationship_states(group_id,user_id,persona_id) WHERE conversation_kind='group';
CREATE UNIQUE INDEX IF NOT EXISTS idx_relationship_states_private_unique ON relationship_states(user_id,persona_id) WHERE conversation_kind='private';
CREATE INDEX IF NOT EXISTS idx_memory_jobs_runnable ON memory_jobs(status,next_run_at,id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_jobs_active_unique ON memory_jobs(job_type,conversation_kind,ifnull(group_id,-1),user_id,persona_id,input_through_id) WHERE status IN ('pending','running');
CREATE INDEX IF NOT EXISTS idx_memory_pending_operations_expiry ON memory_pending_operations(expires_at,id) WHERE consumed_at IS NULL;
INSERT INTO private_memory_schema_meta VALUES(1,3,'legacy');
