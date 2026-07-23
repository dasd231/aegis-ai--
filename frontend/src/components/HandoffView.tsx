import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import type { HandoffTicket, User } from '../types'

type Props = { user: User }
type Filter = 'active' | 'open' | 'in_progress' | 'resolved' | 'closed'

const labels: Record<HandoffTicket['status'], string> = {
  open: '待接管',
  in_progress: '处理中',
  resolved: '已解决',
  closed: '已关闭',
}

const triggerLabels: Record<HandoffTicket['trigger'], string> = {
  manual: '用户主动请求',
  fallback: '系统降级转接',
  complaint: '投诉识别',
  negative_feedback: '负面反馈',
}

export function HandoffView({ user }: Props) {
  const [tickets, setTickets] = useState<HandoffTicket[]>([])
  const [filter, setFilter] = useState<Filter>('active')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      if (filter === 'active') {
        const [open, progressing] = await Promise.all([
          api<HandoffTicket[]>('/admin/handoffs?status=open'),
          api<HandoffTicket[]>('/admin/handoffs?status=in_progress'),
        ])
        setTickets([...open, ...progressing].sort((a, b) => b.priority - a.priority || a.created_at.localeCompare(b.created_at)))
      } else {
        setTickets(await api<HandoffTicket[]>(`/admin/handoffs?status=${filter}`))
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '工单加载失败')
    } finally {
      setLoading(false)
    }
  }, [filter])

  useEffect(() => { void load() }, [load])

  async function update(ticket: HandoffTicket, action: 'claim' | 'resolve' | 'close') {
    let resolution: string | undefined
    if (action === 'resolve') {
      const value = window.prompt('请填写处理结论（会记录在工单中）')
      if (!value?.trim()) return
      resolution = value.trim()
    }
    setBusy(ticket.id)
    setError('')
    try {
      await api(`/admin/handoffs/${ticket.id}`, {
        method: 'PATCH',
        body: JSON.stringify(action === 'claim'
          ? { expected_version: ticket.version, status: 'in_progress', assigned_to: user.id }
          : action === 'resolve'
            ? { expected_version: ticket.version, status: 'resolved', resolution }
            : { expected_version: ticket.version, status: 'closed' }),
      })
      await load()
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) {
        await load()
        setError(`${caught.message}，队列已自动刷新`)
      } else {
        setError(caught instanceof Error ? caught.message : '工单更新失败')
      }
    } finally {
      setBusy(null)
    }
  }

  return (
    <section className="content-view handoff-view">
      <header className="view-header page-title">
        <div><p className="eyebrow">人工协同处置</p><h1>人工接管</h1></div>
        <button className="secondary" onClick={() => void load()} disabled={loading}>刷新队列</button>
      </header>
      {error && <div className="inline-alert"><span>{error}</span><button onClick={() => setError('')}>x</button></div>}
      <div className="handoff-toolbar" role="tablist" aria-label="工单状态">
        {(['active', 'open', 'in_progress', 'resolved', 'closed'] as Filter[]).map((value) => (
          <button key={value} role="tab" aria-selected={filter === value} className={filter === value ? 'active' : ''} onClick={() => setFilter(value)}>
            {value === 'active' ? '当前队列' : labels[value]}
          </button>
        ))}
      </div>
      {loading ? <div className="center-state"><span className="spinner" />加载中</div> : (
        <div className="handoff-list">
          {tickets.map((ticket) => (
            <article key={ticket.id} className="handoff-card">
              <div className="handoff-card-head">
                <div><span className={`status ${ticket.status}`}><i />{labels[ticket.status]}</span><strong>优先级 {ticket.priority} · {ticket.reason}</strong></div>
                <time>{new Date(ticket.created_at).toLocaleString('zh-CN')}</time>
              </div>
              <dl><div><dt>会话编号</dt><dd>{ticket.session_id}</dd></div><div><dt>触发方式</dt><dd>{triggerLabels[ticket.trigger]}</dd></div><div><dt>处理人</dt><dd>{ticket.assigned_to || '未分配'}</dd></div></dl>
              <pre>{ticket.conversation_summary || '暂无对话快照'}</pre>
              {ticket.resolution && <p className="handoff-resolution"><b>处理结论：</b>{ticket.resolution}</p>}
              <div className="handoff-actions">
                {ticket.status === 'open' && <button className="primary" disabled={busy === ticket.id} onClick={() => void update(ticket, 'claim')}>接管</button>}
                {ticket.status === 'in_progress' && <button className="primary" disabled={busy === ticket.id} onClick={() => void update(ticket, 'resolve')}>解决并记录</button>}
                {ticket.status !== 'closed' && <button className="ghost" disabled={busy === ticket.id} onClick={() => void update(ticket, 'close')}>关闭</button>}
              </div>
            </article>
          ))}
          {!tickets.length && <div className="table-empty"><span>◇</span><strong>当前没有工单</strong><p>新工单会按优先级和发起时间排队</p></div>}
        </div>
      )}
    </section>
  )
}
