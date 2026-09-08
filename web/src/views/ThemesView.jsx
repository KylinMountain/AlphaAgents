import { useState } from 'react'
import { DASH, fmtAge, parseList, stageOf } from '../lib/format'

/* Theme lifecycle.
 *
 * Cards, not a table: core_stocks is a list of {code, name, role} and a
 * theme carries a dozen of them. In a table cell that list rendered as the
 * raw JSON string, which was both unreadable and wide enough to squeeze
 * every other column into vertical text. */

const VISIBLE_STOCKS = 8

function StockChip({ stock, leader }) {
  const isLeader = leader || stock.role === '龙头'
  return (
    <span className="watch" style={isLeader ? {
      background: 'var(--amber-soft)', color: 'var(--amber)', fontWeight: 650,
    } : undefined}>
      <strong>{stock.name || stock.code}</strong>
      <span style={{ opacity: 0.7, marginLeft: 5 }}>{stock.code}</span>
    </span>
  )
}

function ThemeCard({ theme }) {
  const [expanded, setExpanded] = useState(false)
  const stage = stageOf(theme.status)
  const stocks = parseList(theme.core_stocks)
  const strength = Number(theme.strength) || 0

  // Leader first, then the rest in their stored order.
  const ordered = [...stocks].sort((a, b) => {
    const al = a.code === theme.leader_code || a.role === '龙头'
    const bl = b.code === theme.leader_code || b.role === '龙头'
    return (bl ? 1 : 0) - (al ? 1 : 0)
  })
  const shown = expanded ? ordered : ordered.slice(0, VISIBLE_STOCKS)
  const hidden = ordered.length - shown.length

  return (
    <article className="card pad" style={{ marginBottom: 12 }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap',
        marginBottom: 10,
      }}>
        <b style={{ fontSize: 15 }}>{theme.name}</b>
        <span className={`stage-chip ${stage.cls}`}>{stage.label}</span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 160 }}>
          <div className="bar" style={{ width: 110 }}>
            <i style={{ width: `${Math.min(100, Math.max(0, strength * 10))}%` }} />
          </div>
          <small style={{ color: 'var(--muted)' }}>强度 {strength}/10</small>
        </div>
        <span style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--muted)' }}>
          {fmtAge(theme.updated_at)}
        </span>
      </div>

      {theme.catalyst && (
        <div className="risk-note" style={{
          background: 'var(--surface-2)', color: 'var(--text-2)', marginBottom: 10,
        }}>
          {theme.catalyst}
        </div>
      )}

      <div className="section-kicker">
        核心标的 {ordered.length ? `· ${ordered.length} 只` : ''}
      </div>
      {ordered.length === 0 ? (
        <p className="empty-note" style={{ paddingTop: 4 }}>该主线尚未记录核心标的</p>
      ) : (
        <div className="watchlist">
          {shown.map((s) => (
            <StockChip key={s.code} stock={s} leader={s.code === theme.leader_code} />
          ))}
          {hidden > 0 && (
            <button className="filter" onClick={() => setExpanded(true)}>
              还有 {hidden} 只
            </button>
          )}
          {expanded && ordered.length > VISIBLE_STOCKS && (
            <button className="filter" onClick={() => setExpanded(false)}>收起</button>
          )}
        </div>
      )}
    </article>
  )
}

export default function ThemesView({ themes }) {
  const byStage = themes.reduce((acc, t) => {
    const { label } = stageOf(t.status)
    acc[label] = (acc[label] || 0) + 1
    return acc
  }, {})

  return (
    <section className="view active">
      <div className="page-head">
        <div>
          <h1>投资主线</h1>
          <p>用生命周期管理市场叙事：观察、活跃、主升、衰退、归档。由复盘与夜扫维护。</p>
        </div>
        <div className="date-note">
          <b>{themes.length} 条活跃主线</b>
          {Object.entries(byStage).map(([k, v]) => `${k} ${v}`).join(' · ') || '已归档的不在此列'}
        </div>
      </div>

      {themes.length === 0 ? (
        <div className="card pad">
          <p className="empty-note">
            尚无主线。主线由 15:30 复盘和 20:00 夜扫创建、加强或降级，
            调度器停止时不会产生新记录。
          </p>
        </div>
      ) : themes.map((t) => <ThemeCard key={t.id ?? t.name} theme={t} />)}
    </section>
  )
}
