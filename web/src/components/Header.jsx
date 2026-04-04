export default function Header({ connected, stages }) {
  const pipeline = stages?.['pipeline']
  const pipelineStatus = pipeline?.status || 'idle'

  const statusMap = {
    idle: { label: 'IDLE', color: 'var(--text-muted)' },
    running: { label: 'RUNNING', color: 'var(--accent-blue)' },
    success: { label: 'DONE', color: 'var(--accent-green)' },
    error: { label: 'ERROR', color: 'var(--accent-red)' },
  }
  const st = statusMap[pipelineStatus] || statusMap.idle

  return (
    <header
      className="flex items-center justify-between px-4 py-2 shrink-0"
      style={{ background: 'var(--bg-secondary)', borderBottom: '1px solid var(--border)' }}
    >
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-2">
          <div className="w-6 h-6 rounded flex items-center justify-center"
               style={{ background: '#3b82f620', border: '1px solid #3b82f640' }}>
            <span style={{ color: 'var(--accent-blue)', fontSize: 12, fontWeight: 900 }}>A</span>
          </div>
          <span className="text-sm font-bold tracking-tight">
            <span style={{ color: 'var(--accent-blue)' }}>Alpha</span>
            <span style={{ color: 'var(--text-primary)' }}>Agents</span>
          </span>
        </div>

        <div className="h-4" style={{ borderLeft: '1px solid var(--border)' }} />

        {/* Pipeline status */}
        <div className="flex items-center gap-2">
          <div className={`w-1.5 h-1.5 rounded-full ${pipelineStatus === 'running' ? 'animate-pulse-dot' : ''}`}
               style={{ background: st.color }} />
          <span className="text-xs font-semibold tracking-wider" style={{ color: st.color }}>
            {st.label}
          </span>
          {pipeline?.message && (
            <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
              {pipeline.message}
            </span>
          )}
        </div>
      </div>

      <div className="flex items-center gap-3">
        <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
          {new Date().toLocaleDateString('zh-CN')}
        </span>
        <div className="flex items-center gap-1.5">
          <div
            className="w-1.5 h-1.5 rounded-full"
            style={{
              background: connected ? 'var(--accent-green)' : 'var(--accent-red)',
              boxShadow: connected ? '0 0 6px #10b981' : '0 0 6px #ef4444',
            }}
          />
          <span className="text-xs" style={{ color: connected ? 'var(--accent-green)' : 'var(--accent-red)' }}>
            {connected ? 'WS' : '...'}
          </span>
        </div>
      </div>
    </header>
  )
}
