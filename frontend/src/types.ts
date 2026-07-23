export type User = {
  id: string
  email: string | null
  phone: string | null
  display_name: string
  role: 'user' | 'admin'
  created_at: string
}

export type AuthResponse = {
  access_token: string
  token_type: string
  expires_in: number
  user: User
}

export type UserProfile = {
  user_id: string
  preferred_name: string | null
  locale: string
  timezone: string
  response_style: 'concise' | 'balanced' | 'detailed'
  memory_consent: boolean
  consent_version: string | null
  profile_learning_consent: boolean
  profile_consent_version: string | null
  facts: UserProfileFact[]
  service_summary: UserServiceSummary | null
  created_at: string
  updated_at: string
}

export type UserProfileFact = {
  id: string
  fact_type: 'device_model' | 'purchase_channel' | 'preferred_contact_channel' | 'service_region'
  value: string
  source: 'explicit' | 'conversation' | 'crm'
  confidence: number
  source_message_id: string | null
  expires_at: string | null
  last_confirmed_at: string
  created_at: string
}

export type UserServiceSummary = {
  total_questions: number
  positive_feedback: number
  negative_feedback: number
  handoff_count: number
  active_task_count: number
  top_intents: string[]
  last_interaction_at: string | null
}

export type Citation = {
  id?: string
  document_id: string | null
  chunk_id?: string
  source_name: string
  excerpt: string
  score: number
  display_order?: number
  source_id?: string
  is_critical_rule?: boolean
  section_path?: string
  parent_expanded?: boolean
}

export type Feedback = { id: string; rating: 1 | -1; comment: string | null }

export type Message = {
  id: string
  role: 'user' | 'assistant'
  content: string
  intent: string | null
  retrieval_status: string | null
  model_name: string | null
  experiment_group?: string | null
  embedding_pool?: string | null
  embedding_version?: string | null
  embedding_model?: string | null
  embedding_dimensions?: number | null
  vector_index_version?: string | null
  prompt_version?: string | null
  prompt_sha256?: string | null
  rag_version?: string | null
  service_version?: string | null
  fallback_used?: boolean
  request_id?: string | null
  ttft_ms?: number | null
  output_tokens?: number | null
  tool_calls?: unknown[]
  finish_reason?: string | null
  latency_ms: number | null
  created_at: string
  citations: Citation[]
  feedback: Feedback | null
  streaming?: boolean
  streaming_status?: string
  error?: boolean
  suggestions?: string[]
  handoff_recommended?: boolean
  handoff_requested?: boolean
  answerability?: 'supported' | 'partial' | 'unsupported'
  claims?: Array<{ text: string; evidence_ids: string[] }>
}

export type Conversation = {
  id: string
  title: string
  status: 'active' | 'archived'
  experiment_group?: string | null
  model_version?: string | null
  embedding_pool?: string | null
  embedding_version?: string | null
  embedding_model?: string | null
  embedding_dimensions?: number | null
  vector_index_version?: string | null
  prompt_version?: string | null
  prompt_sha256?: string | null
  rag_version?: string | null
  service_version?: string | null
  created_at: string
  updated_at: string
}

export type ConversationDetail = Conversation & { messages: Message[] }
export type ConversationPage = { items: Conversation[]; total: number; page: number; page_size: number }

export type KnowledgeBase = {
  id: string
  name: string
  description: string
  is_active: boolean
  created_at: string
}

export type KnowledgeDocument = {
  id: string
  knowledge_base_id: string
  original_name: string
  content_type: string
  size_bytes: number
  status: 'processing' | 'ready' | 'failed' | 'deleting' | 'delete_failed'
  chunk_count: number
  error_message: string | null
  created_at: string
  updated_at: string
}

export type AdminOverview = {
  users: number
  sessions: number
  questions_today: number
  positive_feedback: number
  negative_feedback: number
  daily_questions: Array<{ date: string; count: number }>
}

export type HandoffTicket = {
  id: string
  session_id: string
  user_id: string
  trigger: 'manual' | 'fallback' | 'complaint' | 'negative_feedback'
  reason: string
  priority: number
  status: 'open' | 'in_progress' | 'resolved' | 'closed'
  assigned_to: string | null
  conversation_summary: string | null
  resolution: string | null
  version: number
  created_at: string
  updated_at: string
}

export type AdminConversation = {
  id: string
  user_id: string
  user_display_name: string
  title: string
  status: 'active' | 'archived'
  message_count: number
  created_at: string
  updated_at: string
}

export type AdminConversationPage = { items: AdminConversation[]; total: number; page: number; page_size: number }
export type AdminConversationDetail = AdminConversation & { messages: Message[] }

export type ProviderCredential = {
  id: 'llm_stable' | 'dashscope_ai'
  display_name: string
  category: 'llm' | 'ai_suite'
  provider: string
  base_url: string
  model: string
  enabled: boolean
  has_api_key: boolean
  api_key_fingerprint: string
  validation_status: 'unverified' | 'valid' | 'invalid'
  last_error_code: string | null
  last_validated_at: string | null
  version: number
  updated_at: string
}

export type ProviderCredentialAudit = {
  id: string
  credential_id: string
  admin_id: string | null
  request_id: string | null
  action: string
  from_version: number | null
  to_version: number | null
  changed_fields: string[]
  succeeded: boolean
  error_code: string | null
  created_at: string
}

export type ExperimentControl = {
  id: string
  canary_enabled: boolean
  disabled_reason: string | null
  disabled_at: string | null
  stage: number
  model_canary_percent: number
  embedding_canary_percent: number
  prompt_canary_percent: number
  rag_canary_percent: number
  service_canary_percent: number
  shadow_canary_percent: number
  version: number
  updated_at: string
}

export type ExperimentControlAudit = {
  id: string
  admin_id: string | null
  action: string
  from_version: number
  to_version: number
  previous_config: Record<string, number | boolean>
  next_config: Record<string, number | boolean>
  reason: string
  created_at: string
}

export type StreamEvent =
  | { type: 'ack'; data: { user_message_id: string; session_id: string; usage: { used: number; limit: number }; experiment_group: string } }
  | { type: 'meta'; data: { intent: string; model: string; experiment: ExperimentAssignment } }
  | { type: 'sources'; data: { items: Array<{ source_id: string; document_id: string; document_name: string; excerpt: string; score: number; is_critical_rule: boolean; section_path?: string; parent_expanded?: boolean }> } }
  | { type: 'token'; data: { delta: string } }
  | { type: 'done'; data: { message_id: string; grounded: boolean; evidence_available: boolean; handoff_recommended: boolean; guardrail_filtered_segments: number; answerability: 'supported' | 'partial' | 'unsupported'; input_guardrail: string | null; experiment: ExperimentAssignment; fallback: { used: boolean; effective_versions: ExperimentAssignment }; generation: { ttft_ms: number | null; output_tokens: number | null; tool_calls: unknown[]; finish_reason: string }; claims: Array<{ text: string; evidence_ids: string[] }>; reranking: { attempted: boolean; applied: boolean; reason: string }; knowledge_route: { mode: string; confidence: number; knowledge_base_id: string | null }; suggestions: string[]; timings: { retrieval_ms: number; generation_ms: number; total_ms: number } } }
  | { type: 'error'; data: { code: string; message: string } }
  | { type: 'ping'; data: { ts: number } }

export type ExperimentAssignment = {
  group: string
  model_pool: string
  model_version: string
  embedding_pool: string
  embedding_version: string
  embedding_model: string
  embedding_dimensions: number
  vector_index_version: string
  prompt_version: string
  prompt_sha256: string
  rag_version: string
  service_pool: string
  service_version: string
  buckets: Record<string, number>
  reason: string
}
