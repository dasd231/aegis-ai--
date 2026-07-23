SET NAMES utf8mb4;
SET time_zone = '+00:00';

CREATE TABLE IF NOT EXISTS users (
    id CHAR(36) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
    email VARCHAR(254) NULL,
    phone VARCHAR(20) NULL,
    password_hash VARCHAR(255) NOT NULL,
    display_name VARCHAR(80) NOT NULL DEFAULT '新用户',
    role ENUM('user', 'admin') NOT NULL DEFAULT 'user',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_users_email (email),
    UNIQUE KEY uq_users_phone (phone),
    KEY ix_users_tenant_id (tenant_id),
    CONSTRAINT ck_users_contact CHECK (email IS NOT NULL OR phone IS NOT NULL)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS knowledge_bases (
    id CHAR(36) NOT NULL,
    name VARCHAR(100) NOT NULL,
    description VARCHAR(500) NOT NULL DEFAULT '',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_knowledge_bases_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS conversation_sessions (
    id CHAR(36) NOT NULL,
    user_id CHAR(36) NOT NULL,
    title VARCHAR(120) NOT NULL DEFAULT '新对话',
    status ENUM('active', 'archived') NOT NULL DEFAULT 'active',
    turn_lock_token CHAR(36) NULL,
    turn_lock_expires_at DATETIME(6) NULL,
    experiment_group VARCHAR(20) NULL,
    model_pool VARCHAR(20) NULL,
    model_version VARCHAR(100) NULL,
    embedding_pool VARCHAR(20) NULL,
    embedding_version VARCHAR(80) NULL,
    embedding_model VARCHAR(100) NULL,
    embedding_dimensions INT NULL,
    vector_index_version VARCHAR(120) NULL,
    prompt_version VARCHAR(40) NULL,
    prompt_sha256 CHAR(64) NULL,
    rag_version VARCHAR(40) NULL,
    service_pool VARCHAR(20) NULL,
    service_version VARCHAR(80) NULL,
    experiment_buckets JSON NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY ix_sessions_user_updated (user_id, updated_at),
    CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS knowledge_documents (
    id CHAR(36) NOT NULL,
    knowledge_base_id CHAR(36) NOT NULL,
    original_name VARCHAR(255) NOT NULL,
    storage_path VARCHAR(500) NOT NULL,
    content_type VARCHAR(100) NOT NULL,
    size_bytes INT NOT NULL,
    sha256 CHAR(64) NOT NULL,
    status ENUM('processing', 'ready', 'failed', 'deleting', 'delete_failed') NOT NULL DEFAULT 'processing',
    chunk_count INT NOT NULL DEFAULT 0,
    error_message VARCHAR(500) NULL,
    is_seed BOOLEAN NOT NULL DEFAULT FALSE,
    uploaded_by CHAR(36) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY ix_documents_kb_status (knowledge_base_id, status),
    KEY ix_documents_sha256 (sha256),
    UNIQUE KEY uq_documents_kb_sha256 (knowledge_base_id, sha256),
    CONSTRAINT fk_documents_kb FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id) ON DELETE RESTRICT,
    CONSTRAINT fk_documents_uploader FOREIGN KEY (uploaded_by) REFERENCES users(id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS messages (
    id CHAR(36) NOT NULL,
    session_id CHAR(36) NOT NULL,
    role ENUM('user', 'assistant') NOT NULL,
    content LONGTEXT NOT NULL,
    intent VARCHAR(40) NULL,
    retrieval_status VARCHAR(30) NULL,
    model_name VARCHAR(100) NULL,
    experiment_group VARCHAR(20) NULL,
    embedding_pool VARCHAR(20) NULL,
    embedding_version VARCHAR(80) NULL,
    embedding_model VARCHAR(100) NULL,
    embedding_dimensions INT NULL,
    vector_index_version VARCHAR(120) NULL,
    prompt_version VARCHAR(40) NULL,
    prompt_sha256 CHAR(64) NULL,
    rag_version VARCHAR(40) NULL,
    service_version VARCHAR(80) NULL,
    fallback_used BOOLEAN NOT NULL DEFAULT FALSE,
    request_id VARCHAR(64) NULL,
    ttft_ms INT NULL,
    output_tokens INT NULL,
    tool_calls JSON NOT NULL,
    finish_reason VARCHAR(40) NULL,
    latency_ms INT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY ix_messages_session_created (session_id, created_at),
    KEY ix_messages_request_id (request_id),
    CONSTRAINT fk_messages_session FOREIGN KEY (session_id) REFERENCES conversation_sessions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS message_citations (
    id CHAR(36) NOT NULL,
    message_id CHAR(36) NOT NULL,
    document_id CHAR(36) NULL,
    chunk_id VARCHAR(100) NOT NULL,
    source_name VARCHAR(255) NOT NULL,
    excerpt VARCHAR(500) NOT NULL,
    score INT NOT NULL,
    display_order INT NOT NULL,
    PRIMARY KEY (id),
    KEY ix_citations_message (message_id),
    KEY ix_citations_document (document_id),
    CONSTRAINT fk_citations_message FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
    CONSTRAINT fk_citations_document FOREIGN KEY (document_id) REFERENCES knowledge_documents(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS experiment_controls (
    id VARCHAR(40) NOT NULL,
    canary_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    disabled_reason VARCHAR(500) NULL,
    disabled_at DATETIME(6) NULL,
    stage INT NOT NULL DEFAULT 0,
    model_canary_percent INT NOT NULL DEFAULT 0,
    embedding_canary_percent INT NOT NULL DEFAULT 0,
    prompt_canary_percent INT NOT NULL DEFAULT 0,
    rag_canary_percent INT NOT NULL DEFAULT 0,
    service_canary_percent INT NOT NULL DEFAULT 0,
    shadow_canary_percent INT NOT NULL DEFAULT 0,
    version INT NOT NULL DEFAULT 1,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT IGNORE INTO experiment_controls (id, canary_enabled, version) VALUES ('global', TRUE, 1);

CREATE TABLE IF NOT EXISTS experiment_control_audits (
    id CHAR(36) NOT NULL,
    admin_id CHAR(36) NULL,
    action VARCHAR(30) NOT NULL,
    from_version INT NOT NULL,
    to_version INT NOT NULL,
    previous_config JSON NOT NULL,
    next_config JSON NOT NULL,
    reason VARCHAR(500) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY ix_experiment_audits_created (created_at),
    CONSTRAINT fk_experiment_audits_admin FOREIGN KEY (admin_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS canary_observations (
    id CHAR(36) NOT NULL,
    experiment_group VARCHAR(20) NOT NULL,
    model_version VARCHAR(100) NOT NULL,
    embedding_version VARCHAR(80) NOT NULL,
    embedding_model VARCHAR(100) NOT NULL,
    vector_index_version VARCHAR(120) NOT NULL,
    prompt_version VARCHAR(40) NOT NULL,
    rag_version VARCHAR(40) NOT NULL,
    service_version VARCHAR(80) NOT NULL,
    success BOOLEAN NOT NULL,
    timed_out BOOLEAN NOT NULL DEFAULT FALSE,
    tool_failed BOOLEAN NOT NULL DEFAULT FALSE,
    ttft_ms INT NULL,
    latency_ms INT NOT NULL,
    error_code VARCHAR(80) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY ix_canary_observations_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS shadow_observations (
    id CHAR(36) NOT NULL,
    stable_message_id CHAR(36) NOT NULL,
    model_version VARCHAR(100) NOT NULL,
    embedding_version VARCHAR(80) NOT NULL,
    embedding_model VARCHAR(100) NOT NULL,
    vector_index_version VARCHAR(120) NOT NULL,
    prompt_version VARCHAR(40) NOT NULL,
    prompt_sha256 CHAR(64) NOT NULL,
    rag_version VARCHAR(40) NOT NULL,
    service_version VARCHAR(80) NOT NULL,
    status VARCHAR(30) NOT NULL,
    latency_ms INT NOT NULL,
    stable_answer_sha256 CHAR(64) NOT NULL,
    canary_answer_sha256 CHAR(64) NULL,
    canary_answer_chars INT NOT NULL DEFAULT 0,
    lexical_overlap INT NULL,
    citation_valid BOOLEAN NOT NULL DEFAULT FALSE,
    side_effects_mocked BOOLEAN NOT NULL DEFAULT TRUE,
    error_code VARCHAR(80) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY ix_shadow_observations_created (created_at),
    KEY ix_shadow_observations_stable_message_id (stable_message_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS feedback (
    id CHAR(36) NOT NULL,
    message_id CHAR(36) NOT NULL,
    user_id CHAR(36) NOT NULL,
    rating TINYINT NOT NULL,
    comment VARCHAR(1000) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_feedback_message (message_id),
    KEY ix_feedback_user (user_id),
    CONSTRAINT ck_feedback_rating CHECK (rating IN (-1, 1)),
    CONSTRAINT fk_feedback_message FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
    CONSTRAINT fk_feedback_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS daily_usage (
    id CHAR(36) NOT NULL,
    user_id CHAR(36) NOT NULL,
    usage_date DATE NOT NULL,
    question_count INT NOT NULL DEFAULT 0,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_daily_usage_user_date (user_id, usage_date),
    CONSTRAINT ck_daily_usage_nonnegative CHECK (question_count >= 0),
    CONSTRAINT fk_daily_usage_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id CHAR(36) NOT NULL,
    preferred_name VARCHAR(80) NULL,
    locale VARCHAR(20) NOT NULL DEFAULT 'zh-CN',
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Hong_Kong',
    response_style ENUM('concise', 'balanced', 'detailed') NOT NULL DEFAULT 'balanced',
    memory_consent BOOLEAN NOT NULL DEFAULT FALSE,
    consent_version VARCHAR(40) NULL,
    profile_learning_consent BOOLEAN NOT NULL DEFAULT FALSE,
    profile_consent_version VARCHAR(40) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (user_id),
    CONSTRAINT fk_profiles_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS user_profile_facts (
    id CHAR(36) NOT NULL,
    user_id CHAR(36) NOT NULL,
    fact_type VARCHAR(40) NOT NULL,
    value VARCHAR(160) NOT NULL,
    normalized_value VARCHAR(160) NOT NULL,
    source ENUM('explicit', 'conversation', 'crm') NOT NULL,
    source_message_id CHAR(36) NULL,
    confidence INT NOT NULL,
    expires_at DATETIME(6) NULL,
    revoked_at DATETIME(6) NULL,
    last_confirmed_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_profile_fact_identity (user_id, fact_type, normalized_value),
    KEY ix_profile_facts_user_active (user_id, revoked_at, expires_at),
    KEY ix_user_profile_facts_source_message_id (source_message_id),
    CONSTRAINT ck_profile_fact_confidence CHECK (confidence BETWEEN 0 AND 10000),
    CONSTRAINT fk_profile_facts_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_profile_facts_message FOREIGN KEY (source_message_id) REFERENCES messages(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS session_memories (
    session_id CHAR(36) NOT NULL,
    summary LONGTEXT NOT NULL,
    compacted_message_count INT NOT NULL DEFAULT 0,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (session_id),
    CONSTRAINT fk_memories_session FOREIGN KEY (session_id) REFERENCES conversation_sessions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS conversation_tasks (
    id CHAR(36) NOT NULL,
    session_id CHAR(36) NOT NULL,
    user_id CHAR(36) NOT NULL,
    task_type VARCHAR(50) NOT NULL,
    status ENUM('active', 'completed', 'cancelled') NOT NULL DEFAULT 'active',
    slots JSON NOT NULL,
    version INT NOT NULL DEFAULT 1,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY ix_tasks_session_status (session_id, status),
    KEY ix_tasks_user (user_id),
    CONSTRAINT fk_tasks_session FOREIGN KEY (session_id) REFERENCES conversation_sessions(id) ON DELETE CASCADE,
    CONSTRAINT fk_tasks_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS handoff_tickets (
    id CHAR(36) NOT NULL,
    session_id CHAR(36) NOT NULL,
    active_dedupe_key CHAR(36) NULL,
    user_id CHAR(36) NOT NULL,
    `trigger` ENUM('manual', 'fallback', 'complaint', 'negative_feedback') NOT NULL,
    status ENUM('open', 'in_progress', 'resolved', 'closed') NOT NULL DEFAULT 'open',
    reason VARCHAR(500) NOT NULL,
    priority INT NOT NULL DEFAULT 3,
    assigned_to CHAR(36) NULL,
    conversation_summary LONGTEXT NULL,
    resolution VARCHAR(1000) NULL,
    version INT NOT NULL DEFAULT 1,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_handoffs_active_session (active_dedupe_key),
    KEY ix_handoffs_status_priority_created (status, priority, created_at),
    KEY ix_handoffs_user (user_id),
    KEY ix_handoffs_assignee (assigned_to),
    CONSTRAINT ck_handoffs_priority CHECK (priority BETWEEN 1 AND 5),
    CONSTRAINT ck_handoffs_version CHECK (version >= 1),
    CONSTRAINT fk_handoffs_session FOREIGN KEY (session_id) REFERENCES conversation_sessions(id) ON DELETE CASCADE,
    CONSTRAINT fk_handoffs_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_handoffs_assignee FOREIGN KEY (assigned_to) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS knowledge_ingestion_jobs (
    id CHAR(36) NOT NULL,
    document_id CHAR(36) NOT NULL,
    status ENUM('queued', 'running', 'retry', 'completed', 'dead') NOT NULL DEFAULT 'queued',
    attempts INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 5,
    available_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    lease_owner VARCHAR(100) NULL,
    lease_expires_at DATETIME(6) NULL,
    last_error VARCHAR(500) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_ingestion_document (document_id),
    KEY ix_ingestion_jobs_claim (status, available_at, lease_expires_at),
    CONSTRAINT ck_ingestion_job_attempts CHECK (attempts >= 0 AND max_attempts BETWEEN 1 AND 20),
    CONSTRAINT fk_ingestion_document FOREIGN KEY (document_id) REFERENCES knowledge_documents(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS login_throttles (
    key_hash CHAR(64) NOT NULL,
    failures INT NOT NULL DEFAULT 0,
    window_started_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    locked_until DATETIME(6) NULL,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (key_hash),
    KEY ix_login_throttles_updated (updated_at),
    CONSTRAINT ck_login_throttles_nonnegative CHECK (failures >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS provider_credentials (
    id VARCHAR(40) NOT NULL,
    display_name VARCHAR(80) NOT NULL,
    category VARCHAR(30) NOT NULL,
    provider VARCHAR(40) NOT NULL,
    base_url VARCHAR(500) NOT NULL,
    model VARCHAR(100) NOT NULL,
    api_key_ciphertext LONGTEXT NOT NULL,
    api_key_fingerprint VARCHAR(16) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    validation_status VARCHAR(20) NOT NULL DEFAULT 'unverified',
    last_error_code VARCHAR(80) NULL,
    last_validated_at DATETIME(6) NULL,
    version INT NOT NULL DEFAULT 1,
    updated_by CHAR(36) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY ix_provider_credentials_enabled (enabled),
    CONSTRAINT ck_provider_credentials_version CHECK (version >= 1),
    CONSTRAINT fk_provider_credentials_updater FOREIGN KEY (updated_by) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS provider_credential_audits (
    id CHAR(36) NOT NULL,
    credential_id VARCHAR(40) NOT NULL,
    admin_id CHAR(36) NULL,
    request_id VARCHAR(64) NULL,
    action VARCHAR(30) NOT NULL,
    from_version INT NULL,
    to_version INT NULL,
    changed_fields JSON NOT NULL,
    succeeded BOOLEAN NOT NULL,
    error_code VARCHAR(80) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY ix_provider_audits_credential_created (credential_id, created_at),
    CONSTRAINT fk_provider_audits_admin FOREIGN KEY (admin_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS admin_data_access_audits (
    id CHAR(36) NOT NULL,
    admin_id CHAR(36) NULL,
    action VARCHAR(40) NOT NULL,
    target_type VARCHAR(30) NOT NULL,
    target_id VARCHAR(64) NULL,
    request_id VARCHAR(64) NULL,
    purpose VARCHAR(200) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY ix_admin_data_access_created (created_at),
    CONSTRAINT fk_admin_data_access_admin FOREIGN KEY (admin_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
