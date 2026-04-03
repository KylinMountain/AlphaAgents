const categoryColors = {
  '政策': '#3b82f6',
  '地缘': '#ef4444',
  '宏观': '#eab308',
  '行业': '#22c55e',
  '市场': '#a855f7',
}

function EventBadge({ event }) {
  const color = categoryColors[event.category] || '#64748b'
  return (
    <div className="flex items-center gap-2 py-1">
      <span
        className="text-xs px-1.5 py-0.5 rounded font-medium"
        style={{ background: `${color}22`, color, border: `1px solid ${color}44` }}
      >
        {event.category || '未知'}
      </span>
      <span className="text-xs text-slate-300 flex-1 truncate">{event.event}</span>
      <span className="text-xs text-slate-500">
        {event.importance && `${event.importance}/5`}
      </span>
    </div>
  )
}

function ReportCard({ report, index }) {
  const time = new Date(report.timestamp * 1000).toLocaleString('zh-CN')

  return (
    <div
      className="rounded-lg p-4 mb-3"
      style={{ background: '#1e293b', border: '1px solid #334155' }}
    >
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <span className="text-xs font-mono text-slate-500">#{report.cycle}</span>
          <span className="text-xs text-slate-400">{time}</span>
        </div>
        <span className="text-xs px-2 py-0.5 rounded-full" style={{ background: '#22c55e22', color: '#22c55e' }}>
          {report.event_count} 事件
        </span>
      </div>

      {/* Event summaries */}
      {report.events_summary && (
        <div className="mb-3 border-b border-slate-700 pb-2">
          {report.events_summary.slice(0, 5).map((e, i) => (
            <EventBadge key={i} event={e} />
          ))}
          {report.events_summary.length > 5 && (
            <div className="text-xs text-slate-500 mt-1">
              +{report.events_summary.length - 5} more events
            </div>
          )}
        </div>
      )}

      {/* Report content */}
      <details>
        <summary className="text-xs text-slate-400 cursor-pointer hover:text-slate-200 select-none">
          展开分析报告
        </summary>
        <pre
          className="mt-2 text-xs text-slate-300 whitespace-pre-wrap leading-relaxed overflow-auto"
          style={{ maxHeight: 400, fontFamily: 'inherit' }}
        >
          {report.report}
        </pre>
      </details>
    </div>
  )
}

export default function ReportPanel({ reports }) {
  if (!reports || reports.length === 0) {
    return (
      <div className="text-center py-12 text-slate-500">
        <div className="text-4xl mb-3">📋</div>
        <div className="text-sm">暂无分析报告</div>
        <div className="text-xs mt-1">等待第一轮分析完成...</div>
      </div>
    )
  }

  const sorted = [...reports].reverse()

  return (
    <div>
      {sorted.map((r, i) => (
        <ReportCard key={r.cycle || i} report={r} index={i} />
      ))}
    </div>
  )
}
