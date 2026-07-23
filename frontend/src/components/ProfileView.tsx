import { useEffect, useState, type FormEvent } from 'react'
import { api } from '../api'
import type { UserProfile } from '../types'

const factLabels: Record<string, string> = {
  device_model: '常用设备',
  purchase_channel: '购买渠道',
  preferred_contact_channel: '首选联系渠道',
  service_region: '服务地区',
}

const factValueLabels: Record<string, string> = {
  xiaomi_store: '小米商城 / 官网',
  xiaomi_home: '小米之家 / 线下门店',
  third_party: '第三方销售商',
  enterprise_group: '政企团购',
  chat: '在线聊天',
  phone: '电话',
  email: '邮件',
}


export function ProfileView() {
  const [profile, setProfile] = useState<UserProfile | null>(null)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)
  const [factType, setFactType] = useState('device_model')
  const [factValue, setFactValue] = useState('')

  useEffect(() => {
    api<UserProfile>('/profile').then(setProfile).catch((caught) => setError(caught instanceof Error ? caught.message : '画像加载失败'))
  }, [])

  async function save(event: FormEvent) {
    event.preventDefault()
    if (!profile) return
    setError('')
    try {
      const updated = await api<UserProfile>('/profile', {
        method: 'PUT',
        body: JSON.stringify({
          preferred_name: profile.preferred_name,
          locale: profile.locale,
          timezone: profile.timezone,
          response_style: profile.response_style,
          memory_consent: profile.memory_consent,
          consent_version: profile.memory_consent ? 'privacy-v1' : null,
          profile_learning_consent: profile.profile_learning_consent,
          profile_consent_version: profile.profile_learning_consent ? 'profile-v1' : null,
        }),
      })
      setProfile(updated)
      setSaved(true)
      window.setTimeout(() => setSaved(false), 1800)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '保存失败')
    }
  }

  async function addFact() {
    if (!profile || !factValue.trim()) return
    setError('')
    try {
      await api('/profile/facts', {
        method: 'POST',
        body: JSON.stringify({ fact_type: factType, value: factValue.trim() }),
      })
      setProfile(await api<UserProfile>('/profile'))
      setFactValue('')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '画像事实保存失败')
    }
  }

  async function removeFact(id: string) {
    if (!profile) return
    setError('')
    try {
      await api(`/profile/facts/${id}`, { method: 'DELETE' })
      setProfile({ ...profile, facts: profile.facts.filter((fact) => fact.id !== id) })
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '画像事实删除失败')
    }
  }

  return (
    <section className="content-view">
      <header className="view-header page-title"><div><p className="eyebrow">隐私与记忆管理</p><h1>画像与记忆</h1></div></header>
      {error && <div className="inline-alert" role="alert">{error}<button onClick={() => setError('')}>×</button></div>}
      {!profile ? <div className="center-state"><span className="spinner" />正在载入</div> : (
        <form className="profile-card" onSubmit={save}>
          <h2>显式偏好</h2><p>这里只保存你主动提供的偏好，不推断敏感属性，也不把画像当作企业事实或授权依据。</p>
          <label>称呼<input value={profile.preferred_name ?? ''} maxLength={80} onChange={(event) => setProfile({ ...profile, preferred_name: event.target.value || null })} /></label>
          <label>语言地区<input value={profile.locale} maxLength={20} onChange={(event) => setProfile({ ...profile, locale: event.target.value })} /></label>
          <label>标准时区<input value={profile.timezone} maxLength={64} onChange={(event) => setProfile({ ...profile, timezone: event.target.value })} /></label>
          <label>回答风格<select value={profile.response_style} onChange={(event) => setProfile({ ...profile, response_style: event.target.value as UserProfile['response_style'] })}><option value="concise">简洁</option><option value="balanced">均衡</option><option value="detailed">详细</option></select></label>
          <label className="consent-row"><input type="checkbox" checked={profile.memory_consent} onChange={(event) => setProfile({ ...profile, memory_consent: event.target.checked })} /><span><strong>允许长对话压缩记忆</strong><small>只在当前账号的会话中保存抽取式摘要；关闭后立即清除全部会话摘要，原始聊天记录不受影响。</small></span></label>
          <label className="consent-row"><input type="checkbox" checked={profile.profile_learning_consent} onChange={(event) => setProfile({ ...profile, profile_learning_consent: event.target.checked })} /><span><strong>允许学习低风险服务画像</strong><small>仅从对话中识别设备型号和购买渠道，保留来源、置信度并在 180 天后过期；关闭后立即撤销自动学习事实。</small></span></label>
          <button className="primary" type="submit">{saved ? '已保存' : '保存设置'}</button>

          <div className="profile-section">
            <h2>可核验的服务画像</h2>
            <p>每条事实都有来源、置信度和撤回入口。画像只帮助缩短追问，不替代订单、检测或知识库证据。</p>
            <div className="profile-fact-editor">
              <select value={factType} onChange={(event) => { setFactType(event.target.value); setFactValue('') }}>
                <option value="device_model">常用设备</option>
                <option value="purchase_channel">购买渠道</option>
                <option value="preferred_contact_channel">首选联系渠道</option>
                <option value="service_region">服务地区</option>
              </select>
              {factType === 'purchase_channel' ? (
                <select value={factValue} onChange={(event) => setFactValue(event.target.value)}><option value="">请选择</option><option value="xiaomi_store">小米商城 / 官网</option><option value="xiaomi_home">小米之家 / 线下门店</option><option value="third_party">第三方销售商</option><option value="enterprise_group">政企团购</option></select>
              ) : factType === 'preferred_contact_channel' ? (
                <select value={factValue} onChange={(event) => setFactValue(event.target.value)}><option value="">请选择</option><option value="chat">在线聊天</option><option value="phone">电话</option><option value="email">邮件</option></select>
              ) : <input value={factValue} maxLength={160} placeholder={factType === 'device_model' ? '例如：小米 14 Ultra' : '例如：深圳'} onChange={(event) => setFactValue(event.target.value)} />}
              <button className="secondary" type="button" onClick={addFact}>添加</button>
            </div>
            <div className="profile-fact-list">
              {profile.facts.length === 0 ? <small>尚无服务画像事实</small> : profile.facts.map((fact) => (
                <div className="profile-fact" key={fact.id}>
                  <span><strong>{factLabels[fact.fact_type] ?? fact.fact_type}</strong>{factValueLabels[fact.value] ?? fact.value}</span>
                  <small>{fact.source === 'explicit' ? '本人确认' : fact.source === 'crm' ? 'CRM 同步' : '对话推断'} · 置信度 {(fact.confidence / 100).toFixed(0)}%{fact.expires_at ? ` · ${new Date(fact.expires_at).toLocaleDateString('zh-CN')} 到期` : ''}</small>
                  <button type="button" onClick={() => removeFact(fact.id)}>撤回</button>
                </div>
              ))}
            </div>
          </div>

          {profile.service_summary && <div className="profile-section">
            <h2>服务互动概览</h2>
            <div className="profile-stats"><span><strong>{profile.service_summary.total_questions}</strong>次提问</span><span><strong>{profile.service_summary.positive_feedback}</strong>次满意</span><span><strong>{profile.service_summary.negative_feedback}</strong>次待改进</span><span><strong>{profile.service_summary.handoff_count}</strong>次人工服务</span><span><strong>{profile.service_summary.active_task_count}</strong>个进行中任务</span></div>
            <small>常见意图：{profile.service_summary.top_intents.join('、') || '暂无'}</small>
          </div>}
        </form>
      )}
    </section>
  )
}
