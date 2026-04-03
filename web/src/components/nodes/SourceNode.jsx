import { Handle, Position } from '@xyflow/react'

const statusColors = {
  idle: '#475569',
  running: '#3b82f6',
  success: '#22c55e',
  error: '#ef4444',
}

export default function SourceNode({ data }) {
  const color = statusColors[data.status] || statusColors.idle
  const isRunning = data.status === 'running'

  return (
    <div
      className={`flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs ${isRunning ? 'node-running' : ''}`}
      style={{
        background: '#1e293b',
        border: `1.5px solid ${color}`,
        minWidth: 90,
      }}
      title={data.message}
    >
      <div
        className="rounded-full shrink-0"
        style={{
          width: 7,
          height: 7,
          background: color,
          boxShadow: data.status !== 'idle' ? `0 0 6px ${color}` : 'none',
        }}
      />
      <span className="text-slate-200 whitespace-nowrap">{data.label}</span>
      <Handle type="source" position={Position.Right} style={{ background: color, width: 6, height: 6, border: 'none' }} />
    </div>
  )
}
