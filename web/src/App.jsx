import { useEffect, useState } from 'react'
import Header from './components/Header'
import StatCards from './components/StatCards'
import ReportPanel from './components/ReportPanel'
import ReviewPanel from './components/ReviewPanel'
import SourceGrid from './components/SourceGrid'
import { useDashboard, summarizeReviews } from './hooks/useDashboard'
import { useWebSocket } from './hooks/useWebSocket'

const TABS = [
  { id: 'reports', label: '分析报告' },
  { id: 'reviews', label: '复盘验证' },
]

export default function App() {
  const { reports, reviews, sources, loading, reload } = useDashboard()
  const { connected } = useWebSocket()
  const [tab, setTab] = useState('reports')
  const [dark, setDark] = useState(
    () => localStorage.getItem('aa-theme') === 'dark' ||
      (!localStorage.getItem('aa-theme') &&
        window.matchMedia?.('(prefers-color-scheme: dark)').matches),
  )
  const [refreshing, setRefreshing] = useState(false)

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    try { localStorage.setItem('aa-theme', dark ? 'dark' : 'light') } catch { /* private mode */ }
  }, [dark])

  const summary = summarizeReviews(reviews)
  const sourceStats = {
    total: sources.length,
    up: sources.filter((s) => s.healthy).length,
    down: sources.filter((s) => !s.healthy).length,
  }

  async function handleRefresh() {
    setRefreshing(true)
    await reload()
    setRefreshing(false)
  }

  return (
    <div className="min-h-screen">
      <Header
        connected={connected}
        dark={dark}
        onToggleDark={() => setDark((d) => !d)}
        onRefresh={handleRefresh}
        refreshing={refreshing}
      />

      <main className="mx-auto max-w-7xl px-4 py-4 space-y-4">
        <StatCards
          summary={summary}
          reportCount={reports.length}
          sourceStats={sourceStats}
        />

        <div className="grid lg:grid-cols-[1fr_260px] gap-4 items-start">
          <div className="min-w-0">
            <div className="flex items-center gap-1 mb-3">
              {TABS.map((t) => (
                <button
                  key={t.id}
                  onClick={() => setTab(t.id)}
                  className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors
                    ${tab === t.id
                      ? 'bg-blue-500 text-white shadow-sm'
                      : 'text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-700'}`}
                >
                  {t.label}
                  <span className="ml-1.5 text-xs opacity-70">
                    {t.id === 'reports' ? reports.length : reviews.length}
                  </span>
                </button>
              ))}
            </div>

            {loading ? (
              <div className="card text-center py-12 text-sm text-slate-500 dark:text-slate-400">
                加载中…
              </div>
            ) : tab === 'reports' ? (
              <ReportPanel reports={reports} />
            ) : (
              <ReviewPanel reviews={reviews} />
            )}
          </div>

          <aside className="space-y-4">
            <SourceGrid sources={sources} />
          </aside>
        </div>
      </main>
    </div>
  )
}
