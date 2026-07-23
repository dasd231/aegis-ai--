import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { ProviderCredential, ProviderCredentialAudit } from '../types'

type Draft = {
  provider: string
  base_url: string
  model: string
  api_key: string
  enabled: boolean
  reason: string
}

const statusText = { valid: '验证通过', invalid: '验证失败', unverified: '待验证' }
const actionText: Record<string, string> = {
  validated_update: '验证并更新',
  disabled_draft_update: '保存停用草稿',
  validation_failed: '验证失败',
}

function toDraft(item: ProviderCredential): Draft {
  return {
    provider: item.provider,
    base_url: item.base_url,
    model: item.model,
    api_key: '',
    enabled: item.enabled,
    reason: '管理员控制台配置变更',
  }
}

export function ProviderSettingsView() {
  const [items, setItems] = useState<ProviderCredential[]>([])
  const [drafts, setDrafts] = useState<Record<string, Draft>>({})
  const [audits, setAudits] = useState<ProviderCredentialAudit[]>([])
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    const [credentials, auditRows] = await Promise.all([
      api<ProviderCredential[]>('/admin/providers'),
      api<ProviderCredentialAudit[]>('/admin/providers/audits?limit=20'),
    ])
    setItems(credentials)
    setDrafts(Object.fromEntries(credentials.map((item) => [item.id, toDraft(item)])))
    setAudits(auditRows)
  }, [])

  useEffect(() => {
    load().catch((caught) => setError(caught instanceof Error ? caught.message : '模型配置加载失败'))
  }, [load])

  function setDraft(id: string, patch: Partial<Draft>) {
    setDrafts((current) => ({ ...current, [id]: { ...current[id]!, ...patch } }))
  }

  async function save(item: ProviderCredential, validate: boolean) {
    const draft = drafts[item.id]
    if (!draft) return
    if (draft.enabled && !validate) return setError('启用配置必须先执行在线验证')
    setBusy(item.id)
    setError('')
    setNotice('')
    try {
      await api<ProviderCredential>(`/admin/providers/${item.id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          expected_version: item.version,
          provider: draft.provider,
          base_url: draft.base_url,
          model: draft.model,
          api_key: draft.api_key || null,
          enabled: draft.enabled,
          validate_before_save: validate,
          reason: draft.reason,
        }),
      })
      setNotice(validate ? '在线验证通过，配置已原子更新。' : '停用草稿已保存，未进入业务流量。')
      await load()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '配置更新失败')
    } finally {
      setBusy('')
    }
  }

  return (
    <section className="content-view provider-view">
      <header className="view-header page-title">
        <div><p className="eyebrow">受控推理依赖</p><h1>模型与密钥</h1></div>
        <span className="last-updated">密钥加密保存 · 明文永不回显</span>
      </header>
      {error && <div className="inline-alert" role="alert">{error}<button onClick={() => setError('')}>×</button></div>}
      {notice && <div className="provider-notice" role="status">{notice}</div>}
      <div className="provider-security-note">
        <strong>安全边界</strong>
        <p>新密钥仅从浏览器提交到后端，数据库保存密文与不可逆指纹。启用前必须真实联网验证；验证失败时原配置保持不变。API Host 受服务端白名单限制，避免凭证被转发到未知域名。</p>
      </div>
      <div className="provider-grid">
        {items.map((item) => {
          const draft = drafts[item.id]
          if (!draft) return null
          return (
            <article className="provider-card" key={item.id}>
              <div className="provider-card-head">
                <div><p>{item.category === 'llm' ? '流式生成' : '向量 · 重排 · 多模态'}</p><h2>{item.display_name}</h2></div>
                <span className={`provider-status ${item.validation_status}`}>{statusText[item.validation_status]}</span>
              </div>
              <dl className="provider-meta">
                <div><dt>密钥状态</dt><dd>{item.has_api_key ? `已配置 · 指纹 ${item.api_key_fingerprint}` : '未配置'}</dd></div>
                <div><dt>配置版本</dt><dd>v{item.version}</dd></div>
                <div><dt>最近验证</dt><dd>{item.last_validated_at ? new Date(item.last_validated_at).toLocaleString('zh-CN') : '尚未验证'}</dd></div>
              </dl>
              <div className="provider-form">
                <label>协议标识<input value={draft.provider} onChange={(event) => setDraft(item.id, { provider: event.target.value })} /></label>
                <label>API Host<input value={draft.base_url} onChange={(event) => setDraft(item.id, { base_url: event.target.value })} spellCheck={false} /></label>
                <label>{item.category === 'ai_suite' ? '视觉模型（Embedding / Rerank 由部署版本绑定）' : '模型名称'}<input value={draft.model} onChange={(event) => setDraft(item.id, { model: event.target.value })} spellCheck={false} /></label>
                <label>替换 API Key<input type="password" value={draft.api_key} onChange={(event) => setDraft(item.id, { api_key: event.target.value })} placeholder={item.has_api_key ? '留空则保留现有密钥' : '请输入 API Key'} autoComplete="new-password" /></label>
                <label>变更原因<input value={draft.reason} onChange={(event) => setDraft(item.id, { reason: event.target.value })} maxLength={500} /></label>
                <label className="provider-toggle"><input type="checkbox" checked={draft.enabled} onChange={(event) => setDraft(item.id, { enabled: event.target.checked })} /><span>验证通过后启用业务流量</span></label>
              </div>
              <div className="provider-actions">
                <button className="ghost" disabled={busy === item.id} onClick={() => void save(item, false)}>停用并保存草稿</button>
                <button className="primary" disabled={busy === item.id} onClick={() => void save(item, true)}>{busy === item.id ? '正在验证…' : '验证并保存'}</button>
              </div>
            </article>
          )
        })}
      </div>
      <article className="provider-audit-card">
        <div className="card-heading"><div><h2>最近变更审计</h2><p>不记录 Key 明文，仅记录操作者、版本和字段集合</p></div></div>
        <div className="document-table-wrap">
          <table className="document-table provider-audit-table">
            <thead><tr><th>时间</th><th>配置</th><th>动作</th><th>版本</th><th>变更字段</th><th>结果</th></tr></thead>
            <tbody>{audits.map((row) => <tr key={row.id}>
              <td>{new Date(row.created_at).toLocaleString('zh-CN')}</td><td>{row.credential_id}</td><td>{actionText[row.action] || row.action}</td><td>{row.from_version ?? '-'} → {row.to_version ?? '-'}</td><td>{row.changed_fields.join('、') || '无'}</td><td><span className={`provider-status ${row.succeeded ? 'valid' : 'invalid'}`}>{row.succeeded ? '成功' : row.error_code || '失败'}</span></td>
            </tr>)}</tbody>
          </table>
          {!audits.length && <div className="table-empty"><strong>暂无配置变更</strong></div>}
        </div>
      </article>
    </section>
  )
}
