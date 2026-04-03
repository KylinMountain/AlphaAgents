const SOURCES = [
  { id: 'eastmoney', label: '东方财富', group: 'domestic' },
  { id: 'eastmoney_live', label: '东财7x24', group: 'domestic' },
  { id: 'cls', label: '财联社', group: 'domestic' },
  { id: 'wallstreetcn', label: '华尔街见闻', group: 'domestic' },
  { id: 'jin10', label: '金十数据', group: 'domestic' },
  { id: 'xinhua', label: '新华社', group: 'domestic' },
  { id: 'pboc', label: '人民银行', group: 'domestic' },
  { id: 'world_rss', label: '国际RSS', group: 'intl' },
  { id: 'whitehouse', label: '白宫', group: 'intl' },
  { id: 'fed', label: '美联储', group: 'intl' },
  { id: 'sec', label: 'SEC', group: 'intl' },
  { id: 'social', label: '社交媒体', group: 'intl' },
]

const statusColors = {
  idle: 'var(--text-muted)',
  running: 'var(--accent-blue)',
  success: 'var(--accent-green)',
  error: 'var(--accent-red)',
}

function SourceItem({ source, stage }) {
  const status = stage?.status || 'idle'
  const color = statusColors[status] || statusColors.idle
  const count = stage?.data?.count
  const isRunning = status === 'running'
  const isSkipped = stage?.data?.skipped

  return (
    <div
      className="flex items-center gap-2 px-3 py-1.5 card-hover"
      style={{ borderBottom: '1px solid var(--border)' }}
    >
      <div
        className={`w-1.5 h-1.5 rounded-full shrink-0 ${isRunning ? 'animate-pulse-dot' : ''}`}
        style={{ background: color }}
      />
      <span className="flex-1 text-xs truncate" style={{
        color: isSkipped ? 'var(--text-muted)' : 'var(--text-secondary)',
        textDecoration: isSkipped ? 'line-through' : 'none',
      }}>
        {source.label}
      </span>
      {count !== undefined && (
        <span className="text-xs tabular-nums" style={{ color: count > 0 ? 'var(--accent-green)' : 'var(--text-muted)' }}>
          {count}
        </span>
      )}
      {status === 'error' && !isSkipped && (
        <span className="text-xs" style={{ color: 'var(--accent-red)' }}>!</span>
      )}
    </div>
  )
}

export default function SourceGrid({ stages }) {
  const domestic = SOURCES.filter(s => s.group === 'domestic')
  const intl = SOURCES.filter(s => s.group === 'intl')

  return (
    <div>
      <div className="px-3 py-1.5 text-xs" style={{ color: 'var(--text-muted)', background: 'var(--bg-secondary)' }}>
        国内 ({domestic.length})
      </div>
      {domestic.map(s => (
        <SourceItem key={s.id} source={s} stage={stages[`source_${s.id}`]} />
      ))}

      <div className="px-3 py-1.5 text-xs" style={{ color: 'var(--text-muted)', background: 'var(--bg-secondary)' }}>
        国际 ({intl.length})
      </div>
      {intl.map(s => (
        <SourceItem key={s.id} source={s} stage={stages[`source_${s.id}`]} />
      ))}
    </div>
  )
}
