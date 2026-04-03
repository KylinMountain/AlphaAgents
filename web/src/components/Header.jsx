export default function Header({ connected }) {
  return (
    <header
      className="flex items-center justify-between px-6 py-3"
      style={{ background: '#1e293b', borderBottom: '1px solid #334155' }}
    >
      <div className="flex items-center gap-3">
        <div className="text-xl font-bold tracking-tight">
          <span style={{ color: '#3b82f6' }}>Alpha</span>
          <span className="text-slate-200">Agents</span>
        </div>
        <span className="text-xs text-slate-500 border-l border-slate-600 pl-3">
          新闻驱动自主选股系统
        </span>
      </div>
      <div className="flex items-center gap-2">
        <div
          className="w-2 h-2 rounded-full"
          style={{
            background: connected ? '#22c55e' : '#ef4444',
            boxShadow: connected ? '0 0 6px #22c55e' : '0 0 6px #ef4444',
          }}
        />
        <span className="text-xs text-slate-400">
          {connected ? '已连接' : '连接中...'}
        </span>
      </div>
    </header>
  )
}
