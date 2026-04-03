import { Handle, Position } from '@xyflow/react'

const statusConfig = {
  idle: { color: '#475569', glow: 'none' },
  running: { color: '#a855f7', glow: '0 0 20px rgba(168, 85, 247, 0.3)' },
  success: { color: '#22c55e', glow: '0 0 15px rgba(34, 197, 94, 0.2)' },
  error: { color: '#ef4444', glow: '0 0 15px rgba(239, 68, 68, 0.2)' },
}

const tools = [
  { key: 'search', label: 'search_stocks', icon: '🔍' },
  { key: 'sector', label: 'get_sector_data', icon: '📈' },
  { key: 'filter', label: 'filter_stocks', icon: '🔧' },
  { key: 'watch', label: 'get_watchlist', icon: '👁' },
]

export default function AgentNode({ data }) {
  const cfg = statusConfig[data.status] || statusConfig.idle
  const isRunning = data.status === 'running'

  return (
    <div
      className={`rounded-xl p-4 ${isRunning ? 'node-running' : ''}`}
      style={{
        background: 'linear-gradient(135deg, #1e293b 0%, #0f172a 100%)',
        border: `2px solid ${cfg.color}`,
        minWidth: 200,
        boxShadow: cfg.glow,
      }}
    >
      <Handle type="target" position={Position.Left} style={{ background: cfg.color, width: 8, height: 8, border: 'none' }} />

      {/* Header */}
      <div className="flex items-center gap-2 mb-2">
        <div
          className="w-8 h-8 rounded-lg flex items-center justify-center text-base"
          style={{ background: `${cfg.color}22`, border: `1px solid ${cfg.color}44` }}
        >
          🤖
        </div>
        <div>
          <div className="text-sm font-bold text-slate-100">{data.label}</div>
          <div className="text-xs" style={{ color: cfg.color }}>
            {data.status === 'running' ? '分析中...' : data.status === 'success' ? '已完成' : '待命'}
          </div>
        </div>
      </div>

      {/* Status message */}
      <div className="text-xs text-slate-400 mb-2 leading-relaxed">
        {data.message}
      </div>

      {/* MCP Tools */}
      <div className="border-t border-slate-700 pt-2 mt-1">
        <div className="text-xs text-slate-500 mb-1.5">MCP Tools</div>
        <div className="grid grid-cols-2 gap-1">
          {tools.map(t => (
            <div
              key={t.key}
              className="flex items-center gap-1 text-xs px-1.5 py-0.5 rounded"
              style={{ background: '#0f172a', color: '#64748b', border: '1px solid #1e293b' }}
            >
              <span>{t.icon}</span>
              <span className="truncate" style={{ fontSize: 10 }}>{t.label}</span>
            </div>
          ))}
        </div>
      </div>

      <Handle type="source" position={Position.Right} style={{ background: cfg.color, width: 8, height: 8, border: 'none' }} />
    </div>
  )
}
