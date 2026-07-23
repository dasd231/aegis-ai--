import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { ExperimentControl, ExperimentControlAudit } from '../types'

type PercentField = 'model_canary_percent' | 'embedding_canary_percent' | 'prompt_canary_percent' | 'rag_canary_percent' | 'service_canary_percent' | 'shadow_canary_percent'

const axisLabels: Array<{ key: PercentField; name: string; detail: string }> = [
  { key: 'model_canary_percent', name: 'LLM 模型', detail: '模型名称独立灰度' },
  { key: 'embedding_canary_percent', name: 'Embedding', detail: '模型、维度与物理索引原子绑定' },
  { key: 'prompt_canary_percent', name: 'Prompt', detail: '受审版本与摘要可追溯' },
  { key: 'rag_canary_percent', name: 'RAG 策略', detail: 'TopK、阈值与检索策略' },
  { key: 'service_canary_percent', name: 'vLLM 服务', detail: 'Stable / Canary 独立部署' },
  { key: 'shadow_canary_percent', name: 'Shadow 流量', detail: '只比较，不执行真实副作用' },
]

const stages = [
  'Stage 0 · 离线 Replay', 'Stage 1 · Shadow Traffic', 'Stage 2 · 内部员工',
  'Stage 3 · 1%', 'Stage 4 · 5%', 'Stage 5 · 20%', 'Stage 6 · 50%', 'Stage 7 · 100%',
]

function stagePatch(stage: number): Partial<ExperimentControl> {
  if (stage === 0 || stage === 2) return Object.fromEntries(axisLabels.map(({ key }) => [key, 0]))
  if (stage === 1) return { ...Object.fromEntries(axisLabels.map(({ key }) => [key, 0])), shadow_canary_percent: 100 }
  const percent = [0, 0, 0, 1, 5, 20, 50, 100][stage] ?? 0
  return { model_canary_percent: percent, embedding_canary_percent: percent, prompt_canary_percent: percent, rag_canary_percent: percent, service_canary_percent: percent, shadow_canary_percent: 0 }
}

export function ExperimentSettingsView() {
  const [control, setControl] = useState<ExperimentControl | null>(null)
  const [draft, setDraft] = useState<ExperimentControl | null>(null)
  const [audits, setAudits] = useState<ExperimentControlAudit[]>([])
  const [reason, setReason] = useState('灰度阶段通过质量门后推进')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    const [current, history] = await Promise.all([
      api<ExperimentControl>('/admin/experiments/control'),
      api<ExperimentControlAudit[]>('/admin/experiments/audits?limit=20'),
    ])
    setControl(current)
    setDraft(current)
    setAudits(history)
  }, [])

  useEffect(() => { load().catch((caught) => setError(caught instanceof Error ? caught.message : '灰度配置加载失败')) }, [load])

  async function save(forceStable = false) {
    if (!control || !draft) return
    setBusy(true); setError(''); setNotice('')
    try {
      await api<ExperimentControl>('/admin/experiments/control', {
        method: 'PATCH',
        body: JSON.stringify({
          expected_version: control.version,
          canary_enabled: forceStable ? false : draft.canary_enabled,
          stage: draft.stage,
          ...Object.fromEntries(axisLabels.map(({ key }) => [key, draft[key]])),
          reason: forceStable ? '管理员执行一键回滚至 Stable' : reason,
        }),
      })
      setNotice(forceStable ? '全局 Kill Switch 已关闭 Canary，新会话立即回到 Stable。' : '灰度策略已原子更新；已有会话保持原分组。')
      await load()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '灰度配置更新失败')
    } finally { setBusy(false) }
  }

  if (!draft) return <section className="content-view"><div className="table-empty"><strong>正在加载灰度控制…</strong></div></section>
  return <section className="content-view provider-view experiment-view">
    <header className="view-header page-title"><div><p className="eyebrow">可回滚实验路由</p><h1>灰度发布</h1></div><span className={`provider-status ${draft.canary_enabled ? 'valid' : 'invalid'}`}>{draft.canary_enabled ? 'Canary 已启用' : '仅 Stable'}</span></header>
    {error && <div className="inline-alert" role="alert">{error}<button onClick={() => setError('')}>×</button></div>}
    {notice && <div className="provider-notice" role="status">{notice}</div>}
    <div className="provider-security-note"><strong>会话稳定与索引隔离</strong><p>路由使用稳定 Hash，同一会话不会跨组。Embedding Canary 始终查询自己的索引代际；错误时只回退 Stable，不会把两种向量空间混用。</p></div>
    <article className="provider-audit-card experiment-control-card">
      <div className="experiment-toolbar">
        <label>发布阶段<select value={draft.stage} onChange={(event) => { const stage = Number(event.target.value); setDraft({ ...draft, stage, ...stagePatch(stage) }) }}>{stages.map((name, index) => <option key={name} value={index}>{name}</option>)}</select></label>
        <label className="provider-toggle"><input type="checkbox" checked={draft.canary_enabled} onChange={(event) => setDraft({ ...draft, canary_enabled: event.target.checked })} /><span>允许 Canary 流量</span></label>
      </div>
      <div className="experiment-axis-grid">{axisLabels.map((axis) => <label key={axis.key} className="experiment-axis"><span><strong>{axis.name}</strong><small>{axis.detail}</small></span><input type="number" min="0" max="100" value={draft[axis.key]} onChange={(event) => setDraft({ ...draft, [axis.key]: Math.max(0, Math.min(100, Number(event.target.value))) })} /><b>%</b></label>)}</div>
      <label className="experiment-reason">变更原因<input value={reason} onChange={(event) => setReason(event.target.value)} minLength={3} maxLength={500} /></label>
      <div className="provider-actions"><button className="danger-button" disabled={busy} onClick={() => void save(true)}>一键回滚 Stable</button><button className="primary" disabled={busy || reason.trim().length < 3} onClick={() => void save(false)}>{busy ? '正在提交…' : '应用灰度策略'}</button></div>
    </article>
    <article className="provider-audit-card"><div className="card-heading"><div><h2>发布变更审计</h2><p>记录配置前后快照、操作者、原因与自动回滚</p></div></div><div className="document-table-wrap"><table className="document-table"><thead><tr><th>时间</th><th>动作</th><th>版本</th><th>阶段</th><th>Embedding</th><th>原因</th></tr></thead><tbody>{audits.map((row) => <tr key={row.id}><td>{new Date(row.created_at).toLocaleString('zh-CN')}</td><td>{row.action === 'automatic_rollback' ? '自动回滚' : '人工变更'}</td><td>v{row.from_version} → v{row.to_version}</td><td>Stage {String(row.next_config.stage ?? '-')}</td><td>{String(row.next_config.embedding_canary_percent ?? 0)}%</td><td>{row.reason}</td></tr>)}</tbody></table>{!audits.length && <div className="table-empty"><strong>暂无灰度变更</strong></div>}</div></article>
  </section>
}
