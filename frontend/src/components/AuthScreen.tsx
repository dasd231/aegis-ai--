import { useState, type FormEvent } from 'react'
import { api, tokenStore } from '../api'
import type { AuthResponse, User } from '../types'

const WECHAT_ID = 'Hzh18062855418'
const GITHUB_URL = 'https://github.com/dasd231'

type Props = {
  portal: 'user' | 'admin'
  onAuthenticated: (user: User) => void
}

function ContactCard({ compact = false }: { compact?: boolean }) {
  const [copied, setCopied] = useState(false)

  async function copyWechat() {
    try {
      if (!navigator.clipboard) throw new Error('Clipboard API unavailable')
      await navigator.clipboard.writeText(WECHAT_ID)
    } catch {
      const fallback = document.createElement('textarea')
      fallback.value = WECHAT_ID
      fallback.style.position = 'fixed'
      fallback.style.opacity = '0'
      document.body.appendChild(fallback)
      fallback.select()
      document.execCommand('copy')
      fallback.remove()
    }
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1800)
  }

  return (
    <aside className={`contact-card${compact ? ' compact' : ''}`} aria-label="项目作者联系方式">
      <img className="contact-avatar" src="/contact-avatar.png" alt="项目作者微信头像" />
      <div className="contact-details">
        <p>项目作者 · 快捷联系</p>
        <strong>微信 {WECHAT_ID}</strong>
        <span>GitHub @dasd231</span>
      </div>
      <div className="contact-actions">
        <button type="button" onClick={copyWechat} aria-live="polite">
          {copied ? '已复制微信号 ✓' : '复制微信号'}
        </button>
        <a href={GITHUB_URL} target="_blank" rel="noreferrer" aria-label="在新窗口打开 dasd231 的 GitHub 主页">
          GitHub ↗
        </a>
      </div>
    </aside>
  )
}

export function AuthScreen({ portal, onAuthenticated }: Props) {
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [identityType, setIdentityType] = useState<'email' | 'phone'>('email')
  const [account, setAccount] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setError('')
    setBusy(true)
    try {
      const body =
        mode === 'login'
          ? { account, password }
          : { [identityType]: account, password, display_name: displayName || '新用户' }
      const result = await api<AuthResponse>(`/auth/${mode}`, { method: 'POST', body: JSON.stringify(body) })
      if (portal === 'admin' && result.user.role !== 'admin') {
        throw new Error('此入口仅限管理员账号，请使用用户端入口')
      }
      if (portal === 'user' && result.user.role !== 'user') {
        throw new Error('管理员账号请使用管理端入口')
      }
      tokenStore.set(result.access_token)
      onAuthenticated(result.user)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法登录')
    } finally {
      setBusy(false)
    }
  }

  function switchMode(next: 'login' | 'register') {
    setMode(next)
    setError('')
  }

  return (
    <main className="auth-shell">
      <section className="auth-story" aria-label="产品介绍">
        <div className="brand brand-large"><span className="brand-mark">A</span><span>Aegis</span></div>
        <div className="story-copy">
          <p className="eyebrow">小米客服场景 · 技术演示</p>
          <h1>每一次回答，<br />都有据可查。</h1>
          <p>覆盖设备排障、商城订单和售后服务。先检索、再回答并展示来源；信息不足时明确拒绝猜测。</p>
          <ContactCard />
        </div>
        <div className="story-orb" aria-hidden="true" />
      </section>
      <section className="auth-panel">
        <div className="auth-card">
          <p className="eyebrow">{portal === 'admin' ? 'Aegis 管理控制台' : '欢迎使用 Aegis 智能客服'}</p>
          <h2>{portal === 'admin' ? '管理员登录' : mode === 'login' ? '欢迎回来' : '创建你的账号'}</h2>
          <p className="muted">{portal === 'admin' ? '管理知识、模型、灰度、人工工单与审计。' : mode === 'login' ? '登录后继续你的知识对话。' : '使用邮箱或手机号开始。'}</p>
          <div className="auth-tabs" role="tablist">
            <button className={mode === 'login' ? 'active' : ''} onClick={() => switchMode('login')} type="button">登录</button>
            {portal === 'user' && <button className={mode === 'register' ? 'active' : ''} onClick={() => switchMode('register')} type="button">注册</button>}
          </div>
          <form onSubmit={submit}>
            {mode === 'register' && (
              <>
                <div className="identity-toggle">
                  <button className={identityType === 'email' ? 'active' : ''} type="button" onClick={() => setIdentityType('email')}>邮箱</button>
                  <button className={identityType === 'phone' ? 'active' : ''} type="button" onClick={() => setIdentityType('phone')}>手机号</button>
                </div>
                <label>称呼<input value={displayName} onChange={(event) => setDisplayName(event.target.value)} maxLength={80} placeholder="怎么称呼你" autoComplete="name" /></label>
              </>
            )}
            <label>
              {mode === 'login' ? '邮箱或手机号' : identityType === 'email' ? '邮箱' : '手机号'}
              <input
                value={account}
                onChange={(event) => setAccount(event.target.value)}
                type={mode === 'register' && identityType === 'email' ? 'email' : 'text'}
                placeholder={identityType === 'email' ? 'name@company.com' : '+85255556666'}
                autoComplete="username"
                required
              />
            </label>
            <label>
              密码
              <input value={password} onChange={(event) => setPassword(event.target.value)} type="password" minLength={mode === 'register' ? 8 : 1} maxLength={128} placeholder={mode === 'register' ? '至少 8 位，包含字母和数字' : '输入密码'} autoComplete={mode === 'login' ? 'current-password' : 'new-password'} required />
            </label>
            {error && <div className="form-error" role="alert">{error}</div>}
            <button className="primary wide" disabled={busy} type="submit">{busy ? '请稍候…' : mode === 'login' ? '安全登录' : '创建账号'}</button>
          </form>
          <p className="auth-footnote">
            登录即表示你同意遵守企业数据与账号安全规范。<br />
            <a href={portal === 'admin' ? '/user' : '/admin'}>{portal === 'admin' ? '前往用户端' : '前往管理端'}</a>
          </p>
          <div className="mobile-contact"><ContactCard compact /></div>
        </div>
      </section>
    </main>
  )
}
