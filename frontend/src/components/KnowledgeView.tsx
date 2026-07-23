import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { api } from '../api'
import type { KnowledgeBase, KnowledgeDocument } from '../types'

const statusLabel = { processing: '处理中', ready: '就绪', failed: '入库失败', deleting: '删除中', delete_failed: '删除失败' }

function fileSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export function KnowledgeView() {
  const [bases, setBases] = useState<KnowledgeBase[]>([])
  const [selectedBase, setSelectedBase] = useState('')
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([])
  const [newBaseName, setNewBaseName] = useState('')
  const [newBaseDescription, setNewBaseDescription] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  const fileRef = useRef<HTMLInputElement | null>(null)

  const loadBases = useCallback(async () => {
    const result = await api<KnowledgeBase[]>('/knowledge/bases')
    setBases(result)
    setSelectedBase((current) => current || result[0]?.id || '')
  }, [])

  const loadDocuments = useCallback(async () => {
    if (!selectedBase) return setDocuments([])
    const result = await api<{ items: KnowledgeDocument[] }>(`/knowledge/documents?knowledge_base_id=${encodeURIComponent(selectedBase)}`)
    setDocuments(result.items)
  }, [selectedBase])

  useEffect(() => { loadBases().catch((caught) => setError(caught instanceof Error ? caught.message : '知识库加载失败')) }, [loadBases])
  useEffect(() => { loadDocuments().catch((caught) => setError(caught instanceof Error ? caught.message : '文档加载失败')) }, [loadDocuments])
  useEffect(() => {
    if (!documents.some((document) => document.status === 'processing' || document.status === 'deleting')) return
    const timer = window.setInterval(() => void loadDocuments(), 2500)
    return () => window.clearInterval(timer)
  }, [documents, loadDocuments])

  async function createBase(event: FormEvent) {
    event.preventDefault()
    if (!newBaseName.trim()) return
    try {
      const created = await api<KnowledgeBase>('/knowledge/bases', {
        method: 'POST', body: JSON.stringify({ name: newBaseName, description: newBaseDescription }),
      })
      setNewBaseName('')
      setNewBaseDescription('')
      setShowCreate(false)
      await loadBases()
      setSelectedBase(created.id)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '创建失败')
    }
  }

  async function upload(file: File) {
    if (!selectedBase) return setError('请先创建知识库')
    const form = new FormData()
    form.append('knowledge_base_id', selectedBase)
    form.append('file', file)
    setUploading(true)
    setError('')
    try {
      await api('/knowledge/documents', { method: 'POST', body: form })
      await loadDocuments()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '上传失败')
    } finally {
      setUploading(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  async function remove(document: KnowledgeDocument) {
    if (!window.confirm(`确定删除《${document.original_name}》及其全部向量数据吗？`)) return
    try {
      await api(`/knowledge/documents/${document.id}`, { method: 'DELETE' })
      await loadDocuments()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '删除失败')
    }
  }

  async function retry(document: KnowledgeDocument) {
    try {
      await api(`/knowledge/documents/${document.id}/retry`, { method: 'POST' })
      await loadDocuments()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '重试失败')
    }
  }

  const activeBase = bases.find((base) => base.id === selectedBase)
  return (
    <section className="content-view">
      <header className="view-header page-title">
        <div><p className="eyebrow">企业知识运营</p><h1>知识库管理</h1></div>
        <button className="secondary" onClick={() => setShowCreate(true)}>＋ 新建知识库</button>
      </header>
      {error && <div className="inline-alert" role="alert">{error}<button onClick={() => setError('')}>×</button></div>}
      <div className="knowledge-layout">
        <aside className="base-list">
          <div className="section-caption">知识空间 <span>{bases.length}</span></div>
          {bases.map((base) => (
            <button key={base.id} className={selectedBase === base.id ? 'active' : ''} onClick={() => setSelectedBase(base.id)}>
              <span className="base-icon">◇</span><div><strong>{base.name}</strong><small>{base.description || '暂无描述'}</small></div>
            </button>
          ))}
          {!bases.length && <p className="empty-history">暂无知识库</p>}
        </aside>
        <div className="document-panel">
          <div className="document-toolbar">
            <div><h2>{activeBase?.name ?? '请选择知识库'}</h2><p>{activeBase?.description || '上传资料后，系统将自动拆分并建立向量索引。'}</p></div>
            <label className={`upload-button ${uploading || !selectedBase ? 'disabled' : ''}`}>
              {uploading ? '正在上传…' : '上传文档'}
              <input ref={fileRef} type="file" accept=".txt,.md,.pdf,.png,.jpg,.jpeg,.webp" disabled={uploading || !selectedBase} onChange={(event) => { const file = event.target.files?.[0]; if (file) void upload(file) }} />
            </label>
          </div>
          <div className="drop-hint"><span>⇧</span><div><strong>支持文本、PDF 与常见图片</strong><small>图片和扫描 PDF 需配置多模态模型；单文件最大 15 MB</small></div></div>
          <div className="document-table-wrap">
            <table className="document-table">
              <thead><tr><th>文档</th><th>大小</th><th>状态</th><th>上传时间</th><th aria-label="操作" /></tr></thead>
              <tbody>
                {documents.map((document) => (
                  <tr key={document.id}>
                    <td><span className="file-icon">{document.original_name.split('.').pop()?.toUpperCase()}</span><div><strong>{document.original_name}</strong><small>{document.status === 'ready' ? `${document.chunk_count} 个知识片段` : document.error_message || '等待处理'}</small></div></td>
                    <td>{fileSize(document.size_bytes)}</td>
                    <td><span className={`status ${document.status}`}><i />{statusLabel[document.status]}</span></td>
                    <td>{new Date(document.created_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}</td>
                    <td>{document.status === 'failed' && <button className="icon-button" onClick={() => void retry(document)} aria-label={`重试 ${document.original_name}`}>↻</button>}<button className="icon-button danger" onClick={() => void remove(document)} disabled={document.status === 'deleting'} aria-label={`删除 ${document.original_name}`}>×</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!documents.length && <div className="table-empty"><span>▤</span><strong>这里还没有文档</strong><p>上传第一份资料，建立可追溯的企业知识。</p></div>}
          </div>
        </div>
      </div>
      {showCreate && (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setShowCreate(false)}>
          <form className="modal" onSubmit={createBase} onMouseDown={(event) => event.stopPropagation()}>
            <button className="modal-close" type="button" onClick={() => setShowCreate(false)}>×</button>
            <p className="eyebrow">创建知识空间</p><h2>新建知识库</h2>
            <label>名称<input value={newBaseName} onChange={(event) => setNewBaseName(event.target.value)} maxLength={100} autoFocus required /></label>
            <label>描述<textarea value={newBaseDescription} onChange={(event) => setNewBaseDescription(event.target.value)} maxLength={500} rows={3} /></label>
            <div className="modal-actions"><button type="button" className="ghost" onClick={() => setShowCreate(false)}>取消</button><button className="primary" type="submit">创建</button></div>
          </form>
        </div>
      )}
    </section>
  )
}
