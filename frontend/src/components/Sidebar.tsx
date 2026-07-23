import type { Conversation, User } from '../types'

export type ViewName = 'chat' | 'knowledge' | 'providers' | 'experiments' | 'handoffs' | 'audit' | 'analytics' | 'profile'

type Props = {
  user: User
  sessions: Conversation[]
  activeSessionId: string | null
  view: ViewName
  open: boolean
  onClose: () => void
  onView: (view: ViewName) => void
  onSelectSession: (id: string) => void
  onNew: () => void
  onLogout: () => void
}

function timeLabel(value: string) {
  const date = new Date(value)
  const today = new Date()
  return date.toDateString() === today.toDateString()
    ? date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    : date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
}

export function Sidebar(props: Props) {
  const nav = [
    { id: 'chat' as const, icon: '✦', label: '智能问答' },
    ...(props.user.role === 'admin' ? [
      { id: 'knowledge' as const, icon: '▤', label: '知识库' },
      { id: 'providers' as const, icon: '⌘', label: '模型与密钥' },
      { id: 'experiments' as const, icon: '◫', label: '灰度发布' },
      { id: 'handoffs' as const, icon: '◈', label: '人工接管' },
      { id: 'audit' as const, icon: '◎', label: '会话审计' },
      { id: 'analytics' as const, icon: '⌁', label: '数据概览' },
    ] : []),
    { id: 'profile' as const, icon: '◉', label: '画像与记忆' },
  ]
  return (
    <>
      {props.open && <button className="mobile-scrim" aria-label="关闭菜单" onClick={props.onClose} />}
      <aside className={`sidebar ${props.open ? 'open' : ''}`}>
        <div className="brand"><span className="brand-mark">A</span><span>Aegis</span></div>
        <button className="new-chat" onClick={props.onNew}><span>＋</span> 发起新对话</button>
        <nav className="main-nav" aria-label="主导航">
          {nav.map((item) => (
            <button key={item.id} className={props.view === item.id ? 'active' : ''} onClick={() => props.onView(item.id)}>
              <span aria-hidden="true">{item.icon}</span>{item.label}
            </button>
          ))}
        </nav>
        <div className="history-head"><span>最近会话</span><span>{props.sessions.length}</span></div>
        <div className="session-list">
          {props.sessions.map((session) => (
            <button key={session.id} className={props.activeSessionId === session.id && props.view === 'chat' ? 'active' : ''} onClick={() => props.onSelectSession(session.id)} title={session.title}>
              <span className="session-dot" /><span className="session-title">{session.title}</span><time>{timeLabel(session.updated_at)}</time>
            </button>
          ))}
          {!props.sessions.length && <p className="empty-history">还没有会话</p>}
        </div>
        <div className="user-card">
          <div className="avatar">{props.user.display_name.slice(0, 1).toUpperCase()}</div>
          <div><strong>{props.user.display_name}</strong><span>{props.user.role === 'admin' ? '管理员' : '成员'}</span></div>
          <button onClick={props.onLogout} aria-label="退出登录" title="退出登录">↗</button>
        </div>
      </aside>
    </>
  )
}
