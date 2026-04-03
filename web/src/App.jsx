import { useWebSocket } from './hooks/useWebSocket'
import Header from './components/Header'
import PipelineFlow from './components/PipelineFlow'
import ReportPanel from './components/ReportPanel'

function PipelineStatus({ stages }) {
  const pipeline = stages['pipeline']
  if (!pipeline) return null

  const statusLabels = {
    idle: '空闲',
    running: '运行中',
    success: '已完成',
    error: '出错',
  }

  const statusColors = {
    idle: '#64748b',
    running: '#3b82f6',
    success: '#22c55e',
    error: '#ef4444',
  }

  const color = statusColors[pipeline.status] || statusColors.idle

  return (
    <div
      className="flex items-center gap-3 px-4 py-2 rounded-lg text-sm"
      style={{ background: `${color}11`, border: `1px solid ${color}33` }}
    >
      <div
        className="w-2.5 h-2.5 rounded-full shrink-0"
        style={{ background: color, boxShadow: `0 0 8px ${color}` }}
      />
      <span style={{ color }}>{statusLabels[pipeline.status] || pipeline.status}</span>
      <span className="text-slate-400 text-xs">{pipeline.message}</span>
    </div>
  )
}

export default function App() {
  const { stages, reports, connected } = useWebSocket()

  return (
    <div className="min-h-screen flex flex-col" style={{ background: '#0f172a' }}>
      <Header connected={connected} />

      <main className="flex-1 flex flex-col lg:flex-row gap-0">
        {/* Left: Pipeline visualization */}
        <div className="flex-1 flex flex-col" style={{ minWidth: 0 }}>
          <div className="px-6 pt-4 pb-2 flex items-center justify-between">
            <h2 className="text-base font-semibold text-slate-200">Pipeline</h2>
            <PipelineStatus stages={stages} />
          </div>
          <div className="flex-1 mx-4 mb-4 rounded-xl overflow-hidden" style={{ border: '1px solid #334155' }}>
            <PipelineFlow stages={stages} />
          </div>
        </div>

        {/* Right: Reports panel */}
        <div
          className="w-full lg:w-[420px] shrink-0 overflow-y-auto flex flex-col"
          style={{ borderLeft: '1px solid #334155', maxHeight: 'calc(100vh - 52px)' }}
        >
          <div className="px-4 pt-4 pb-2 sticky top-0 z-10" style={{ background: '#0f172a' }}>
            <h2 className="text-base font-semibold text-slate-200">分析报告</h2>
          </div>
          <div className="px-4 pb-4 flex-1">
            <ReportPanel reports={reports} />
          </div>
        </div>
      </main>
    </div>
  )
}
