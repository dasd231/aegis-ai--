import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { api, streamChat } from '../api'
import type { Citation, ConversationDetail, Message, StreamEvent } from '../types'

type Props = {
  sessionId: string | null
  ensureSession: () => Promise<string>
  onSessionEstablished: (id: string) => void
  onSessionsChanged: () => void
}

const starters = ['小米手机充不进电，应该怎么排查？', '小米商城订单在哪里查看物流？', '手机退货、换货和保修期限分别多久？']

export function ChatView({ sessionId, ensureSession, onSessionEstablished, onSessionsChanged }: Props) {
  const [messages, setMessages] = useState<Message[]>([])
  const [question, setQuestion] = useState('')
  const [loading, setLoading] = useState(false)
  const [sending, setSending] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [feedbackEditor, setFeedbackEditor] = useState<string | null>(null)
  const [feedbackDraft, setFeedbackDraft] = useState('')
  const [feedbackSaving, setFeedbackSaving] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const endRef = useRef<HTMLDivElement | null>(null)
  const latestSessionIdRef = useRef(sessionId)
  latestSessionIdRef.current = sessionId

  useEffect(() => {
    abortRef.current?.abort()
    setMessages([])
    setLoadError('')
    if (!sessionId) return
    let active = true
    setLoading(true)
    api<ConversationDetail>(`/sessions/${sessionId}`)
      .then((detail) => active && setMessages(detail.messages))
      .catch((error) => active && setLoadError(error instanceof Error ? error.message : '会话加载失败'))
      .finally(() => active && setLoading(false))
    return () => { active = false }
  }, [sessionId])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: sending ? 'auto' : 'smooth' })
  }, [messages, sending])

  function updateAssistant(id: string, updater: (message: Message) => Message) {
    setMessages((current) => current.map((message) => (message.id === id ? updater(message) : message)))
  }

  async function send(explicit?: string) {
    const text = (explicit ?? question).trim()
    if (!text || sending || text.length > 500) return
    setQuestion('')
    setSending(true)
    const now = new Date().toISOString()
    const userTempId = `user-${crypto.randomUUID()}`
    const assistantTempId = `assistant-${crypto.randomUUID()}`
    setMessages((current) => [
      ...current,
      { id: userTempId, role: 'user', content: text, intent: null, retrieval_status: null, model_name: null, latency_ms: null, created_at: now, citations: [], feedback: null },
      { id: assistantTempId, role: 'assistant', content: '', intent: null, retrieval_status: null, model_name: null, latency_ms: null, created_at: now, citations: [], feedback: null, streaming: true, streaming_status: '正在连接知识服务…' },
    ])
    const controller = new AbortController()
    abortRef.current = controller
    let actualSessionId: string | null = null
    try {
      actualSessionId = sessionId ?? await ensureSession()
      await streamChat(
        { session_id: actualSessionId, question: text },
        (event: StreamEvent) => {
          if (event.type === 'ack') {
            setMessages((current) => current.map((message) => message.id === userTempId ? { ...message, id: event.data.user_message_id } : message))
            updateAssistant(assistantTempId, (message) => ({ ...message, streaming_status: '问题已接收，正在检索客服知识…' }))
          } else if (event.type === 'meta') {
            updateAssistant(assistantTempId, (message) => ({
              ...message,
              intent: event.data.intent,
              model_name: event.data.model,
              experiment_group: event.data.experiment.group,
              embedding_pool: event.data.experiment.embedding_pool,
              embedding_version: event.data.experiment.embedding_version,
              embedding_model: event.data.experiment.embedding_model,
              embedding_dimensions: event.data.experiment.embedding_dimensions,
              vector_index_version: event.data.experiment.vector_index_version,
              prompt_version: event.data.experiment.prompt_version,
              prompt_sha256: event.data.experiment.prompt_sha256,
              rag_version: event.data.experiment.rag_version,
              service_version: event.data.experiment.service_version,
              streaming_status: '正在筛选可靠知识片段…',
            }))
          } else if (event.type === 'sources') {
            const citations: Citation[] = event.data.items.map((source, index) => ({
              document_id: source.document_id,
              source_name: source.document_name,
              excerpt: source.excerpt,
              score: Math.round(source.score * 10000),
              display_order: index + 1,
              source_id: source.source_id,
              is_critical_rule: source.is_critical_rule,
              section_path: source.section_path,
              parent_expanded: source.parent_expanded,
            }))
            updateAssistant(assistantTempId, (message) => ({ ...message, citations, streaming_status: citations.length ? `已找到 ${citations.length} 条可靠来源，正在组织回答…` : '未找到可靠来源，正在生成安全说明…' }))
          } else if (event.type === 'token') {
            updateAssistant(assistantTempId, (message) => ({ ...message, content: message.content + event.data.delta, streaming_status: undefined }))
          } else if (event.type === 'done') {
            updateAssistant(assistantTempId, (message) => ({ ...message, id: event.data.message_id, streaming: false, streaming_status: undefined, latency_ms: event.data.timings.total_ms, suggestions: event.data.suggestions, handoff_recommended: event.data.handoff_recommended, answerability: event.data.answerability, claims: event.data.claims, experiment_group: event.data.experiment.group, embedding_pool: event.data.fallback.effective_versions.embedding_pool, embedding_version: event.data.fallback.effective_versions.embedding_version, embedding_model: event.data.fallback.effective_versions.embedding_model, embedding_dimensions: event.data.fallback.effective_versions.embedding_dimensions, vector_index_version: event.data.fallback.effective_versions.vector_index_version, prompt_version: event.data.fallback.effective_versions.prompt_version, prompt_sha256: event.data.fallback.effective_versions.prompt_sha256, rag_version: event.data.fallback.effective_versions.rag_version, service_version: event.data.fallback.effective_versions.service_version, fallback_used: event.data.fallback.used, retrieval_status: event.data.evidence_available ? (event.data.answerability === 'partial' || event.data.guardrail_filtered_segments ? 'grounded_filtered' : 'matched') : 'empty' }))
          } else if (event.type === 'error') {
            updateAssistant(assistantTempId, (message) => ({ ...message, content: event.data.message, streaming: false, streaming_status: undefined, error: true }))
          }
        },
        controller.signal,
      )
    } catch (caught) {
      const aborted = caught instanceof DOMException && caught.name === 'AbortError'
      const failure = caught instanceof Error ? caught.message : '发送失败'
      updateAssistant(assistantTempId, (message) => ({
        ...message,
        content: aborted
          ? message.content || '已停止生成。'
          : message.content
            ? `${message.content}\n\n（${failure}）`
            : failure,
        streaming: false,
        streaming_status: undefined,
        error: !aborted,
      }))
    } finally {
      abortRef.current = null
      setSending(false)
      if (!sessionId && actualSessionId && latestSessionIdRef.current === null) {
        onSessionEstablished(actualSessionId)
      }
      onSessionsChanged()
    }
  }

  async function rate(message: Message, rating: 1 | -1, comment: string | null = null) {
    if (message.streaming || message.id.startsWith('assistant-')) return false
    try {
      const feedback = await api<{ id: string; rating: 1 | -1; comment: string | null }>(`/messages/${message.id}/feedback`, {
        method: 'PUT', body: JSON.stringify({ rating, comment }),
      })
      setMessages((current) => current.map((item) => item.id === message.id ? { ...item, feedback, handoff_recommended: rating === -1 || item.handoff_recommended } : item))
      return true
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : '反馈提交失败')
      return false
    }
  }

  function editFeedback(message: Message) {
    setFeedbackDraft(message.feedback?.comment ?? '')
    setFeedbackEditor(message.id)
  }

  async function saveFeedback(message: Message) {
    setFeedbackSaving(true)
    try {
      const saved = await rate(message, message.feedback?.rating ?? -1, feedbackDraft.trim() || null)
      if (saved) {
        setFeedbackEditor(null)
        setFeedbackDraft('')
      }
    } finally {
      setFeedbackSaving(false)
    }
  }

  async function requestHandoff(message: Message) {
    if (!sessionId || message.handoff_requested) return
    try {
      await api('/handoffs', {
        method: 'POST',
        body: JSON.stringify({
          session_id: sessionId,
          trigger: 'manual',
          reason: message.handoff_recommended ? '智能助手建议人工接管，用户已确认' : '用户主动请求人工客服',
          priority: message.intent === '投诉' ? 4 : 3,
        }),
      })
      setMessages((current) => current.map((item) => item.id === message.id ? { ...item, handoff_requested: true } : item))
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : '人工接管请求失败')
    }
  }

  function keyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      void send()
    }
  }

  const empty = !loading && messages.length === 0
  return (
    <section className="chat-view">
      <header className="view-header chat-header">
        <div><span className="online-dot" /><span>小米场景智能客服在线</span></div>
        <p>设备、订单与售后问题，回答均附知识来源</p>
      </header>
      <div className={`message-scroll ${empty ? 'welcome-state' : ''}`}>
        {loading && <div className="center-state"><span className="spinner" />正在载入会话</div>}
        {loadError && <div className="inline-alert" role="alert">{loadError}<button onClick={() => setLoadError('')}>×</button></div>}
        {empty && (
          <div className="welcome-copy">
            <div className="assistant-emblem">✦</div>
            <p className="eyebrow">小米场景演示客服</p>
            <h1>您好，请问需要什么帮助？</h1>
            <p>可以咨询设备排障、商城订单、退换维修。涉及实际权益时，请以官方实时政策为准。</p>
            <div className="starter-grid">
              {starters.map((starter) => <button key={starter} onClick={() => void send(starter)}><span>↗</span>{starter}</button>)}
            </div>
          </div>
        )}
        {!empty && messages.map((message) => (
          <article key={message.id} className={`message ${message.role} ${message.error ? 'error' : ''}`}>
            {message.role === 'assistant' && <div className="message-avatar">A</div>}
            <div className="message-body">
              <div className="message-meta">
                <strong>{message.role === 'assistant' ? '小米场景智能客服' : '你'}</strong>
                {message.intent && <span className="intent-pill">{message.intent}</span>}
                {message.role === 'assistant' && message.answerability && <span className={`evidence-pill ${message.answerability}`}>{message.answerability === 'supported' ? '证据充分' : message.answerability === 'partial' ? '部分依据' : '知识不足'}</span>}
                {message.role === 'assistant' && message.latency_ms !== null && message.latency_ms !== undefined && <span className="latency-pill">{(message.latency_ms / 1000).toFixed(2)} 秒</span>}
                <time>{new Date(message.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time>
              </div>
              <div className="message-content" aria-live={message.streaming ? 'polite' : undefined}>{message.content || (message.streaming ? <span className="thinking"><i /><i /><i /><em>{message.streaming_status || '正在处理…'}</em></span> : '')}{message.streaming && message.content && <span className="cursor" />}</div>
              {!!message.citations.length && (
                <details className="sources" open>
                  <summary>知识来源 <span>{message.citations.length}</span></summary>
                  <div className="source-list">
                    {message.citations.map((citation, index) => (
                      <div className="source-card" key={`${citation.document_id}-${citation.chunk_id ?? index}`}>
                        <span className="source-index">{citation.source_id ?? `S${index + 1}`}</span>
                        <div><strong>{citation.source_name}{citation.section_path ? ` · ${citation.section_path}` : ''}</strong><p>{citation.excerpt}</p></div>
                        {citation.is_critical_rule && <span className="rule-badge">关键规则</span>}
                      </div>
                    ))}
                  </div>
                </details>
              )}
              {message.role === 'assistant' && !message.streaming && !message.error && (
                <div className="answer-actions">
                  <span>这个回答有帮助吗？</span>
                  <button className={message.feedback?.rating === 1 ? 'active' : ''} onClick={() => { setFeedbackEditor(null); void rate(message, 1, message.feedback?.comment ?? null) }} aria-label="有帮助">↑</button>
                  <button className={message.feedback?.rating === -1 ? 'active negative' : ''} onClick={() => { void rate(message, -1, message.feedback?.comment ?? null); editFeedback(message) }} aria-label="没帮助">↓</button>
                  {message.feedback && <button className="feedback-comment-button" onClick={() => editFeedback(message)}>{message.feedback.comment ? '编辑反馈' : '补充反馈'}</button>}
                  <button className="handoff-button" onClick={() => void requestHandoff(message)} disabled={!sessionId || message.handoff_requested}>{message.handoff_requested ? '已提交人工' : message.handoff_recommended ? '建议转人工' : '转人工'}</button>
                </div>
              )}
              {feedbackEditor === message.id && (
                <form className="feedback-editor" onSubmit={(event) => { event.preventDefault(); void saveFeedback(message) }}>
                  <label htmlFor={`feedback-${message.id}`}>补充反馈（可选）</label>
                  <textarea id={`feedback-${message.id}`} value={feedbackDraft} onChange={(event) => setFeedbackDraft(event.target.value)} maxLength={1000} rows={3} placeholder="例如：哪条信息不准确，或还缺少什么内容" autoFocus />
                  <div><span>{feedbackDraft.length}/1000</span><button type="button" className="ghost" onClick={() => setFeedbackEditor(null)}>取消</button><button type="submit" className="primary" disabled={feedbackSaving}>{feedbackSaving ? '保存中…' : '保存反馈'}</button></div>
                </form>
              )}
              {!!message.suggestions?.length && (
                <div className="suggestions">{message.suggestions.map((suggestion) => <button key={suggestion} onClick={() => void send(suggestion)}>{suggestion}<span>→</span></button>)}</div>
              )}
            </div>
          </article>
        ))}
        <div ref={endRef} />
      </div>
      <form className="composer-wrap" onSubmit={(event: FormEvent) => { event.preventDefault(); void send() }}>
        <div className={`composer ${question.length > 500 ? 'invalid' : ''}`}>
          <textarea value={question} onChange={(event) => setQuestion(event.target.value)} onKeyDown={keyDown} placeholder="输入你的问题，按回车发送，按住 Shift 再回车换行" rows={1} disabled={sending} aria-label="输入问题" />
          <span className="char-count">{question.length}/500</span>
          {sending ? <button className="stop-button" type="button" onClick={() => abortRef.current?.abort()} aria-label="停止生成">■</button> : <button className="send-button" type="submit" disabled={!question.trim() || question.length > 500} aria-label="发送">↑</button>}
        </div>
        <p>本页面为技术演示，不代表小米官方客服；重要权益请以官方实时页面为准。</p>
      </form>
    </section>
  )
}
