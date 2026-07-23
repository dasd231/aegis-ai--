import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { AdminConversationDetail, AdminConversationPage } from '../types'

export function ConversationAuditView() {
  const [page, setPage] = useState(1)
  const [data, setData] = useState<AdminConversationPage | null>(null)
  const [detail, setDetail] = useState<AdminConversationDetail | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try { setData(await api<AdminConversationPage>(`/admin/sessions?page=${page}&page_size=20`)) }
    catch (caught) { setError(caught instanceof Error ? caught.message : '会话加载失败') }
    finally { setLoading(false) }
  }, [page])

  useEffect(() => { void load() }, [load])

  async function inspect(id: string) {
    setError('')
    try { setDetail(await api<AdminConversationDetail>(`/admin/sessions/${id}`)) }
    catch (caught) { setError(caught instanceof Error ? caught.message : '会话读取失败') }
  }

  const totalPages = Math.max(1, Math.ceil((data?.total ?? 0) / 20))
  return (
    <section className="content-view audit-view">
      <header className="view-header page-title">
        <div><p className="eyebrow">受控权限审计</p><h1>会话审计</h1></div>
        <span className="last-updated">管理员读取会写入服务审计日志</span>
      </header>
      {error && <div className="inline-alert"><span>{error}</span><button onClick={() => setError('')}>x</button></div>}
      {loading ? <div className="center-state"><span className="spinner" />加载中</div> : (
        <div className="audit-panel">
          <div className="document-table-wrap">
            <table className="document-table audit-table">
              <thead><tr><th>会话</th><th>用户</th><th>消息</th><th>更新时间</th><th>操作</th></tr></thead>
              <tbody>{data?.items.map((session) => (
                <tr key={session.id}>
                  <td><div><strong>{session.title}</strong><small>会话编号：{session.id}</small></div></td>
                  <td><div><strong>{session.user_display_name}</strong><small>用户编号：{session.user_id}</small></div></td>
                  <td>{session.message_count}</td><td>{new Date(session.updated_at).toLocaleString('zh-CN')}</td>
                  <td><button className="secondary" onClick={() => void inspect(session.id)}>查看</button></td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          {!data?.items.length && <div className="table-empty"><span>◎</span><strong>暂无会话</strong></div>}
          <div className="audit-pagination"><button className="ghost" disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>上一页</button><span>{page} / {totalPages}、共 {data?.total ?? 0} 条</span><button className="ghost" disabled={page >= totalPages} onClick={() => setPage((value) => value + 1)}>下一页</button></div>
        </div>
      )}
      {detail && <div className="modal-backdrop" onMouseDown={(event) => { if (event.currentTarget === event.target) setDetail(null) }}>
        <article className="modal audit-detail"><button className="modal-close" onClick={() => setDetail(null)}>×</button><p className="eyebrow">授权审计视图</p><h2>{detail.title}</h2><p className="audit-owner">{detail.user_display_name} · 用户编号：{detail.user_id} · {detail.message_count} 条消息</p>
          <div className="audit-messages">{detail.messages.map((message) => <div key={message.id} className={`audit-message ${message.role}`}><strong>{message.role === 'user' ? '用户' : '智能助手'} · {new Date(message.created_at).toLocaleString('zh-CN')}</strong><p>{message.content}</p>{message.citations.length > 0 && <small>来源：{message.citations.map((item) => item.source_name).join('、')}</small>}</div>)}</div>
        </article>
      </div>}
    </section>
  )
}
