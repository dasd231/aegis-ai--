import { useCallback, useEffect, useState } from 'react'
import { api, tokenStore } from './api'
import { AnalyticsView } from './components/AnalyticsView'
import { AuthScreen } from './components/AuthScreen'
import { ChatView } from './components/ChatView'
import { KnowledgeView } from './components/KnowledgeView'
import { HandoffView } from './components/HandoffView'
import { ConversationAuditView } from './components/ConversationAuditView'
import { ProfileView } from './components/ProfileView'
import { ProviderSettingsView } from './components/ProviderSettingsView'
import { ExperimentSettingsView } from './components/ExperimentSettingsView'
import { Sidebar, type ViewName } from './components/Sidebar'
import type { Conversation, ConversationPage, User } from './types'

export default function App() {
  const portal = window.location.pathname.startsWith('/admin') ? 'admin' : 'user'
  const [user, setUser] = useState<User | null>(null)
  const [authLoading, setAuthLoading] = useState(Boolean(tokenStore.get()))
  const [sessions, setSessions] = useState<Conversation[]>([])
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [view, setView] = useState<ViewName>(portal === 'admin' ? 'analytics' : 'chat')
  const [sidebarOpen, setSidebarOpen] = useState(false)

  const logout = useCallback(() => {
    tokenStore.clear()
    setUser(null)
    setSessions([])
    setSessionId(null)
  }, [])

  const loadSessions = useCallback(async () => {
    if (!tokenStore.get()) return
    const result = await api<ConversationPage>('/sessions?page=1&page_size=50')
    setSessions(result.items)
  }, [])

  useEffect(() => {
    const expired = () => logout()
    window.addEventListener('aegis:auth-expired', expired)
    return () => window.removeEventListener('aegis:auth-expired', expired)
  }, [logout])

  useEffect(() => {
    if (!tokenStore.get()) return setAuthLoading(false)
    let active = true
    api<User>('/auth/me')
      .then((result) => {
        if (!active) return
        if (result.role !== portal) {
          logout()
          return
        }
        setUser(result)
      })
      .catch(() => { if (active) logout() })
      .finally(() => { if (active) setAuthLoading(false) })
    return () => { active = false }
  }, [logout])

  useEffect(() => { if (user) void loadSessions() }, [user, loadSessions])

  async function ensureSession() {
    if (sessionId) return sessionId
    const created = await api<Conversation>('/sessions', { method: 'POST', body: JSON.stringify({ title: '新对话' }) })
    setSessions((current) => [created, ...current])
    return created.id
  }

  function selectSession(id: string) {
    setSessionId(id)
    setView('chat')
    setSidebarOpen(false)
  }

  function newChat() {
    setSessionId(null)
    setView('chat')
    setSidebarOpen(false)
  }

  if (authLoading) return <div className="app-loading"><span className="brand-mark">A</span><div className="loading-line" /></div>
  if (!user) return <AuthScreen portal={portal} onAuthenticated={setUser} />

  return (
    <div className="app-shell">
      <Sidebar
        user={user}
        sessions={sessions}
        activeSessionId={sessionId}
        view={view}
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        onView={(next) => { setView(next); setSidebarOpen(false) }}
        onSelectSession={selectSession}
        onNew={newChat}
        onLogout={logout}
      />
      <main className="workspace">
        <button className="mobile-menu-button" onClick={() => setSidebarOpen(true)} aria-label="打开菜单">☰</button>
        {view === 'chat' && <ChatView sessionId={sessionId} ensureSession={ensureSession} onSessionEstablished={setSessionId} onSessionsChanged={() => void loadSessions()} />}
        {view === 'knowledge' && user.role === 'admin' && <KnowledgeView />}
        {view === 'providers' && user.role === 'admin' && <ProviderSettingsView />}
        {view === 'experiments' && user.role === 'admin' && <ExperimentSettingsView />}
        {view === 'handoffs' && user.role === 'admin' && <HandoffView user={user} />}
        {view === 'audit' && user.role === 'admin' && <ConversationAuditView />}
        {view === 'analytics' && user.role === 'admin' && <AnalyticsView />}
        {view === 'profile' && <ProfileView />}
      </main>
    </div>
  )
}
