import { useState } from 'react'
import { useWebSocket } from './hooks/useWebSocket'
import Header from './components/Header'
import SourceGrid from './components/SourceGrid'
import EventTimeline from './components/EventTimeline'
import ReportPanel from './components/ReportPanel'
import StatsBar from './components/StatsBar'

export default function App() {
  const { stages, reports, events, connected } = useWebSocket()
  const [activeTab, setActiveTab] = useState('timeline') // timeline | reports

  // Extract digest events from stages
  const digestData = stages['digest']?.data || {}
  const digestEvents = digestData.events || []

  return (
    <div className="h-screen flex flex-col overflow-hidden" style={{ background: 'var(--bg-primary)' }}>
      <Header connected={connected} stages={stages} />
      <StatsBar stages={stages} reports={reports} />

      <main className="flex-1 flex min-h-0">
        {/* Left sidebar — Source health grid */}
        <div
          className="w-[220px] shrink-0 overflow-y-auto"
          style={{ borderRight: '1px solid var(--border)' }}
        >
          <div className="px-3 py-2 text-xs font-semibold uppercase tracking-wider"
               style={{ color: 'var(--text-muted)' }}>
            数据源
          </div>
          <SourceGrid stages={stages} />
        </div>

        {/* Center — Event timeline + Digest events */}
        <div className="flex-1 flex flex-col min-w-0">
          {/* Tab bar */}
          <div className="flex items-center gap-0 px-4"
               style={{ borderBottom: '1px solid var(--border)' }}>
            <TabButton
              active={activeTab === 'timeline'}
              onClick={() => setActiveTab('timeline')}
              label="事件流"
              count={events.length}
            />
            <TabButton
              active={activeTab === 'reports'}
              onClick={() => setActiveTab('reports')}
              label="分析报告"
              count={reports.length}
            />
          </div>

          <div className="flex-1 overflow-y-auto">
            {activeTab === 'timeline' ? (
              <EventTimeline
                events={events}
                digestEvents={digestEvents}
                stages={stages}
              />
            ) : (
              <ReportPanel reports={reports} />
            )}
          </div>
        </div>

        {/* Right — Latest report quick view */}
        <div
          className="w-[400px] shrink-0 overflow-y-auto hidden xl:block"
          style={{ borderLeft: '1px solid var(--border)' }}
        >
          <div className="px-4 py-2 flex items-center justify-between"
               style={{ borderBottom: '1px solid var(--border)' }}>
            <span className="text-xs font-semibold uppercase tracking-wider"
                  style={{ color: 'var(--text-muted)' }}>
              最新报告
            </span>
            {reports.length > 0 && (
              <span className="text-xs" style={{ color: 'var(--accent-green)' }}>
                #{reports[reports.length - 1]?.cycle}
              </span>
            )}
          </div>
          <LatestReport report={reports[reports.length - 1]} />
        </div>
      </main>
    </div>
  )
}

function TabButton({ active, onClick, label, count }) {
  return (
    <button
      onClick={onClick}
      className="px-4 py-2.5 text-xs font-medium transition-colors relative"
      style={{
        color: active ? 'var(--accent-blue)' : 'var(--text-muted)',
        background: 'transparent',
        border: 'none',
        cursor: 'pointer',
      }}
    >
      {label}
      {count > 0 && (
        <span className="ml-1.5 px-1.5 py-0.5 rounded-full text-xs"
              style={{
                background: active ? '#3b82f622' : '#1e2d42',
                color: active ? 'var(--accent-blue)' : 'var(--text-muted)',
                fontSize: 10,
              }}>
          {count}
        </span>
      )}
      {active && (
        <div className="absolute bottom-0 left-2 right-2 h-[2px] rounded-full"
             style={{ background: 'var(--accent-blue)' }} />
      )}
    </button>
  )
}

function LatestReport({ report }) {
  if (!report) {
    return (
      <div className="p-4 text-center" style={{ color: 'var(--text-muted)' }}>
        <div className="text-2xl mb-2 opacity-30">⏳</div>
        <div className="text-xs">等待第一轮分析...</div>
      </div>
    )
  }

  const time = new Date(report.timestamp * 1000).toLocaleString('zh-CN')

  return (
    <div className="p-4 space-y-3">
      <div className="text-xs" style={{ color: 'var(--text-muted)' }}>{time}</div>

      {/* Events summary */}
      {report.events_summary?.map((e, i) => (
        <div key={i} className="flex items-start gap-2 py-1">
          <span className={`tag-${e.category || '未知'} text-xs px-1.5 py-0.5 rounded shrink-0`}>
            {e.category || '?'}
          </span>
          <span className="text-xs leading-relaxed" style={{ color: 'var(--text-secondary)' }}>
            {e.event}
          </span>
          {e.target_market && e.target_market !== 'both' && (
            <span className="text-xs px-1 rounded shrink-0"
                  style={{
                    background: e.target_market === 'stock' ? '#3b82f615' : '#f59e0b15',
                    color: e.target_market === 'stock' ? '#60a5fa' : '#fbbf24',
                    fontSize: 10,
                  }}>
              {e.target_market === 'stock' ? '股' : '期'}
            </span>
          )}
        </div>
      ))}

      {/* Stock report */}
      {report.report && (
        <div className="mt-3">
          <div className="text-xs font-semibold mb-1.5 flex items-center gap-1.5"
               style={{ color: 'var(--accent-blue)' }}>
            <span className="w-1 h-3 rounded-full" style={{ background: 'var(--accent-blue)' }} />
            股票分析
          </div>
          <pre className="text-xs leading-relaxed whitespace-pre-wrap overflow-auto"
               style={{ color: 'var(--text-secondary)', maxHeight: 300 }}>
            {report.report}
          </pre>
        </div>
      )}

      {/* Futures report */}
      {report.futures_report && (
        <div className="mt-3">
          <div className="text-xs font-semibold mb-1.5 flex items-center gap-1.5"
               style={{ color: 'var(--accent-yellow)' }}>
            <span className="w-1 h-3 rounded-full" style={{ background: 'var(--accent-yellow)' }} />
            期货分析
          </div>
          <pre className="text-xs leading-relaxed whitespace-pre-wrap overflow-auto"
               style={{ color: 'var(--text-secondary)', maxHeight: 300 }}>
            {report.futures_report}
          </pre>
        </div>
      )}
    </div>
  )
}
