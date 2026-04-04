export default function StatsBar({ stages, reports }) {
  const fetch = stages['fetch']?.data || {}
  const digest = stages['digest']?.data || {}
  const latest = reports[reports.length - 1]

  const stats = [
    { label: '数据源', value: fetch.sources ? Object.keys(fetch.sources).length : '-', color: 'var(--accent-cyan)' },
    { label: '原始新闻', value: fetch.total ?? '-', color: 'var(--accent-blue)' },
    { label: '筛选事件', value: digest.event_count ?? '-', color: 'var(--accent-green)' },
    { label: '分析轮次', value: latest?.cycle ?? '-', color: 'var(--accent-purple)' },
    {
      label: '路由',
      value: latest?.routes
        ? `股${latest.routes.stock || 0} / 期${latest.routes.futures || 0}`
        : '-',
      color: 'var(--accent-yellow)'
    },
  ]

  return (
    <div
      className="flex items-center gap-0 px-4 py-1.5 shrink-0"
      style={{ background: 'var(--bg-secondary)', borderBottom: '1px solid var(--border)' }}
    >
      {stats.map((s, i) => (
        <div key={s.label} className="flex items-center gap-0">
          {i > 0 && (
            <div className="mx-3 h-3" style={{ borderLeft: '1px solid var(--border)' }} />
          )}
          <span className="text-xs mr-1.5" style={{ color: 'var(--text-muted)' }}>{s.label}</span>
          <span className="text-xs font-semibold tabular-nums" style={{ color: s.color }}>{s.value}</span>
        </div>
      ))}
    </div>
  )
}
