import { useEffect, useState, type CSSProperties } from 'react'
import { api } from '../api'
import type { AdminOverview } from '../types'

export function AnalyticsView() {
  const [data, setData] = useState<AdminOverview | null>(null)
  const [error, setError] = useState('')
  useEffect(() => { api<AdminOverview>('/admin/overview').then(setData).catch((caught) => setError(caught instanceof Error ? caught.message : '数据加载失败')) }, [])
  const max = Math.max(1, ...(data?.daily_questions.map((item) => item.count) ?? [1]))
  const chartWidth = 600
  const chartHeight = 210
  const chartPadding = 24
  const chartPoints = (data?.daily_questions ?? []).map((item, index, items) => ({
    ...item,
    x: items.length <= 1 ? chartWidth / 2 : chartPadding + index * ((chartWidth - chartPadding * 2) / (items.length - 1)),
    y: chartHeight - chartPadding - (item.count / max) * (chartHeight - chartPadding * 2),
  }))
  const linePoints = chartPoints.map((item) => `${item.x},${item.y}`).join(' ')
  const areaPoints = chartPoints.length ? `${chartPoints[0]!.x},${chartHeight - chartPadding} ${linePoints} ${chartPoints[chartPoints.length - 1]!.x},${chartHeight - chartPadding}` : ''
  const totalFeedback = (data?.positive_feedback ?? 0) + (data?.negative_feedback ?? 0)
  const satisfaction = totalFeedback ? Math.round(((data?.positive_feedback ?? 0) / totalFeedback) * 100) : 0
  return (
    <section className="content-view analytics-view">
      <header className="view-header page-title"><div><p className="eyebrow">服务运营分析</p><h1>数据概览</h1></div><span className="last-updated">实时业务数据</span></header>
      {error && <div className="inline-alert">{error}</div>}
      {!data ? <div className="center-state"><span className="spinner" />正在汇总数据</div> : (
        <>
          <div className="metric-grid">
            <article><span className="metric-icon violet">◇</span><div><small>注册用户</small><strong>{data.users.toLocaleString()}</strong><p>累计账号</p></div></article>
            <article><span className="metric-icon blue">⌁</span><div><small>历史会话</small><strong>{data.sessions.toLocaleString()}</strong><p>全量会话</p></div></article>
            <article><span className="metric-icon green">✦</span><div><small>今日问答</small><strong>{data.questions_today.toLocaleString()}</strong><p>业务时区口径</p></div></article>
            <article><span className="metric-icon amber">↑</span><div><small>回答满意度</small><strong>{satisfaction}%</strong><p>{totalFeedback} 条有效反馈</p></div></article>
          </div>
          <div className="analytics-grid">
            <article className="chart-card">
              <div className="card-heading"><div><h2>近 7 日问答量</h2><p>按自然日聚合的用户提问</p></div></div>
              <div className="line-chart" role="img" aria-label={`近 7 日问答量折线图，最高单日 ${max} 次`}>
                <svg viewBox={`0 0 ${chartWidth} ${chartHeight}`} preserveAspectRatio="none" aria-hidden="true">
                  {[0, 1, 2, 3].map((line) => <line key={line} x1={chartPadding} x2={chartWidth - chartPadding} y1={chartPadding + line * ((chartHeight - chartPadding * 2) / 3)} y2={chartPadding + line * ((chartHeight - chartPadding * 2) / 3)} className="chart-grid-line" />)}
                  {areaPoints && <polygon points={areaPoints} className="chart-area" />}
                  {linePoints && <polyline points={linePoints} className="chart-line" />}
                  {chartPoints.map((item) => <circle key={item.date} cx={item.x} cy={item.y} r="5" className="chart-point"><title>{item.date}：{item.count} 次</title></circle>)}
                </svg>
                <div className="line-chart-values">{chartPoints.map((item) => <span key={item.date}><strong>{item.count}</strong><time>{item.date.slice(5)}</time></span>)}</div>
              </div>
            </article>
            <article className="feedback-card">
              <div className="card-heading"><div><h2>用户反馈</h2><p>累计点赞与点踩</p></div></div>
              <div className="satisfaction-ring" style={{ '--value': `${satisfaction * 3.6}deg` } as CSSProperties}><div><strong>{satisfaction}%</strong><span>满意</span></div></div>
              <div className="feedback-legend"><span><i className="positive" />有帮助 <strong>{data.positive_feedback}</strong></span><span><i className="negative" />需改进 <strong>{data.negative_feedback}</strong></span></div>
            </article>
          </div>
        </>
      )}
    </section>
  )
}
