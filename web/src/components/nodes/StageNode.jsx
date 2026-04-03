import { Handle, Position } from '@xyflow/react'

const statusConfig = {
  idle: { color: '#475569', bg: '#1e293b' },
  running: { color: '#3b82f6', bg: '#1e293b' },
  success: { color: '#22c55e', bg: '#1e293b' },
  error: { color: '#ef4444', bg: '#1e293b' },
}

export default function StageNode({ data }) {
  const cfg = statusConfig[data.status] || statusConfig.idle
  const isRunning = data.status === 'running'

  return (
    <div
      className={`rounded-lg p-3 ${isRunning ? 'node-running' : ''}`}
      style={{
        background: cfg.bg,
        border: `2px solid ${cfg.color}`,
        minWidth: 160,
        boxShadow: data.status !== 'idle' ? `0 0 15px ${cfg.color}33` : 'none',
      }}
    >
      <Handle type="target" position={Position.Left} style={{ background: cfg.color, width: 8, height: 8, border: 'none' }} />

      <div className="flex items-center gap-2 mb-1.5">
        <span className="text-lg">{data.icon}</span>
        <span className="text-sm font-semibold text-slate-200">{data.label}</span>
      </div>

      <div className="text-xs text-slate-400 leading-relaxed" style={{ maxWidth: 180 }}>
        {data.message}
      </div>

      {data.stats?.categories && (
        <div className="flex flex-wrap gap-1 mt-2">
          {Object.entries(data.stats.categories).map(([cat, count]) => (
            <span
              key={cat}
              className="text-xs px-1.5 py-0.5 rounded"
              style={{ background: '#334155', color: '#94a3b8' }}
            >
              {cat} {count}
            </span>
          ))}
        </div>
      )}

      <Handle type="source" position={Position.Right} style={{ background: cfg.color, width: 8, height: 8, border: 'none' }} />
    </div>
  )
}
