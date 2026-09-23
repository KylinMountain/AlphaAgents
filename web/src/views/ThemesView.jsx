import { useState } from 'react'
import { DASH, fmtClock, fmtPct, fmtYi, parseList, stageOf, trendClass } from '../lib/format'

/* Theme lifecycle — and what each line is doing *today*.
 *
 * Two clocks on one card, and the page used to show only the slow one. The
 * lifecycle (stage, 0–10 strength) moves over days on purpose; the board's
 * change and net flow move every snapshot. The grey line under each title was
 * ``catalyst``, the sentence from the day the line was found, so 小金属概念
 * read "净流入57.1亿" on a day its board was −53亿, and "20 分钟前" was the
 * scorer touching the row, not anything changing. Today's print now leads,
 * the origin is labelled as the origin, and the default order is today's.
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

function TodayLine({ today }) {
  if (!today) {
    return <span className="soft">今日无板块快照（主线名与概念板块对不上）</span>
  }
  return (
    <>
      <span>今日 <b className={trendClass(today.change_pct)}>{fmtPct(today.change_pct, 2)}</b></span>
      <span>净流入 <b className={trendClass(today.net_flow_yi)}>{fmtYi(today.net_flow_yi)}</b></span>
      {today.leader ? (
        <span>领涨 {today.leader}
          {today.leader_change_pct != null
            ? <span className={trendClass(today.leader_change_pct)}> {fmtPct(today.leader_change_pct, 1)}</span>
            : null}
        </span>
      ) : null}
    </>
  )
}

function ThemeCard({ theme }) {
  const [expanded, setExpanded] = useState(false)
  const stage = stageOf(theme.status)
  const stocks = parseList(theme.core_stocks)
  const strength = Number(theme.strength) || 0
  const trend = theme.trend_score == null ? null : Number(theme.trend_score)

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
          <small style={{ color: 'var(--muted)' }}>累计强度 {strength}/10</small>
        </div>
        <small style={{ color: 'var(--muted)' }}>
          今日分 {trend == null ? DASH : trend.toFixed(2)}
        </small>
      </div>

      <div className="theme-today">
        <TodayLine today={theme.today} />
      </div>

      {theme.catalyst && (
        <p className="theme-origin">
          起因（{String(theme.created_at || '').slice(0, 10) || DASH} 发现）：{theme.catalyst}
        </p>
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

const SORTS = {
  today: { label: '按今日', key: (t) => (t.trend_score == null ? -1 : Number(t.trend_score)) },
  flow: { label: '按今日净流入', key: (t) => (t.today ? Number(t.today.net_flow_yi) : -Infinity) },
  life: { label: '按累计强度', key: (t) => Number(t.strength) || 0 },
}

export default function ThemesView({ themes, themesMeta }) {
  const [sort, setSort] = useState('today')
  const byStage = themes.reduce((acc, t) => {
    const { label } = stageOf(t.status)
    acc[label] = (acc[label] || 0) + 1
    return acc
  }, {})
  const ordered = [...themes].sort((a, b) => SORTS[sort].key(b) - SORTS[sort].key(a))
  const untracked = themesMeta?.untracked_leaders || []

  return (
    <section className="view active">
      <div className="page-head">
        <div>
          <h1>投资主线</h1>
          <p>
            生命周期（观察、活跃、主升、衰退、归档）由复盘与夜扫维护，按天变化；
            今日涨跌与净流入来自板块快照
            {themesMeta?.snapshot_at ? `（${fmtClock(themesMeta.snapshot_at)}）` : ''}。
          </p>
        </div>
        <div className="date-note">
          <b>{themes.length} 条活跃主线</b>
          {Object.entries(byStage).map(([k, v]) => `${k} ${v}`).join(' · ') || '已归档的不在此列'}
        </div>
      </div>

      {untracked.length ? (
        <div className="card pad theme-untracked">
          <span className="section-kicker">今日资金前列、不在跟踪里</span>
          <div className="watchlist">
            {untracked.map((u) => (
              <span key={u.name} className="sector-chip">
                {u.name} <b className={trendClass(u.net_flow_yi)}>{fmtYi(u.net_flow_yi)}</b>
                {' '}<span className={trendClass(u.change_pct)}>{fmtPct(u.change_pct, 2)}</span>
              </span>
            ))}
          </div>
        </div>
      ) : null}

      {themes.length ? (
        <div className="filter-group" style={{ marginBottom: 12 }}>
          {Object.entries(SORTS).map(([k, v]) => (
            <button key={k} className={`filter${sort === k ? ' active' : ''}`}
                    onClick={() => setSort(k)}>{v.label}</button>
          ))}
        </div>
      ) : null}

      {themes.length === 0 ? (
        <div className="card pad">
          <p className="empty-note">
            尚无主线。主线由 15:30 复盘和 20:00 夜扫创建、加强或降级，
            调度器停止时不会产生新记录。
          </p>
        </div>
      ) : ordered.map((t) => <ThemeCard key={t.id ?? t.name} theme={t} />)}
    </section>
  )
}
