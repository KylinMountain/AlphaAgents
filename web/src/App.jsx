import { useEffect, useState } from 'react'
import './styles/terminal.css'
import { useDashboard } from './hooks/useDashboard'
import { useWebSocket } from './hooks/useWebSocket'
import { DASH, fmtClock } from './lib/format'
import HomeView from './views/HomeView'
import RealtimeView from './views/RealtimeView'
import AnomaliesView from './views/AnomaliesView'
import ThemesView from './views/ThemesView'
import GraphView from './views/GraphView'
import ReportsView from './views/ReportsView'
import MemoryView from './views/MemoryView'
import PortfolioView from './views/PortfolioView'
import SystemView from './views/SystemView'

const NAV = [
  { group: 'Workspace', items: [
    { id: 'home', icon: '◈', label: '今日总览' },
    { id: 'realtime', icon: '◉', label: '实时资讯', count: 'news' },
    { id: 'anomalies', icon: '⚡', label: '实时异动', count: 'signals' },
    { id: 'themes', icon: '⌁', label: '投资主线', count: 'themes' },
    { id: 'portfolio', icon: '▦', label: '虚拟持仓', count: 'positions' },
    { id: 'graph', icon: '◇', label: '事件图谱' },
  ] },
  { group: 'Research', items: [
    { id: 'reports', icon: '▤', label: '分析报告', count: 'reports' },
    { id: 'memory', icon: '◎', label: '记忆与验证' },
    { id: 'system', icon: '⚙', label: '系统状态' },
  ] },
]

const TITLES = Object.fromEntries(
  NAV.flatMap((g) => g.items).map((i) => [i.id, i.label]),
)

/** 09:30–11:30 and 13:00–15:00 on weekdays. The browser does not know the
 *  holiday calendar, so this reports the schedule, never "交易中" — a label
 *  that would be wrong on every public holiday. */
function sessionLabel(now) {
  const day = now.getDay()
  if (day === 0 || day === 6) return { text: '非交易日', live: false }
  const hm = now.getHours() * 100 + now.getMinutes()
  const open = (hm >= 930 && hm <= 1130) || (hm >= 1300 && hm <= 1500)
  return open
    ? { text: '交易时段 09:30–15:00', live: true }
    : { text: '休市', live: false }
}

/* The view lives in the URL hash so a page can be linked, reloaded and
   screenshotted directly. Held in memory only, every view but the default
   was unreachable from the outside — including to a headless browser
   checking the deploy. */
function viewFromHash() {
  const id = window.location.hash.replace(/^#\/?/, '')
  return TITLES[id] ? id : 'home'
}

export default function App() {
  const d = useDashboard()
  const { connected } = useWebSocket()
  const [view, setView] = useState(viewFromHash)
  const [now, setNow] = useState(() => new Date())
  const [dark, setDark] = useState(() => {
    try {
      const saved = localStorage.getItem('aa-theme')
      if (saved) return saved === 'dark'
    } catch { /* private mode */ }
    return window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false
  })

  useEffect(() => {
    const root = document.documentElement
    root.setAttribute('data-theme', dark ? 'dark' : 'light')
    // Markdown prose styling still comes from Tailwind's class-based dark
    // variant, so both switches have to move together.
    root.classList.toggle('dark', dark)
    try { localStorage.setItem('aa-theme', dark ? 'dark' : 'light') } catch { /* ignore */ }
  }, [dark])

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 30000)
    return () => clearInterval(id)
  }, [])

  useEffect(() => {
    const onHash = () => setView(viewFromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const counts = {
    positions: (d.portfolio?.positions?.length || 0)
      + (d.portfolio?.pending?.length || 0),
    news: d.news.length,
    signals: d.signals.length,
    themes: d.themes.length,
    reports: d.reports.length,
  }
  const session = sessionLabel(now)
  const healthy = d.sources.filter((s) => s.healthy).length
  const tasksOk = new Set(
    d.activity.filter((a) => a.status === 'ok').map((a) => a.task),
  ).size
  const tasksSeen = new Set(d.activity.map((a) => a.task).filter(Boolean)).size

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="logo" />
          <div><strong>AlphaAgents</strong><span>Intelligence Terminal</span></div>
        </div>
        {NAV.map((g) => (
          <div className="nav-group" key={g.group}>
            <div className="nav-label">{g.group}</div>
            {g.items.map((it) => (
              <button key={it.id}
                      className={`nav-item ${view === it.id ? 'active' : ''}`}
                      onClick={() => {
                        window.location.hash = `#/${it.id}`
                        setView(it.id)
                        window.scrollTo({ top: 0 })
                      }}>
                <span className="ico">{it.icon}</span>
                <span>{it.label}</span>
                {it.count && counts[it.count] > 0 && (
                  <span className="badge">{counts[it.count]}</span>
                )}
              </button>
            ))}
          </div>
        ))}
        <div className="sidebar-foot">
          <div className="status-line">
            <span className="status-left">
              <i className="dot" style={connected ? undefined : { background: 'var(--muted)' }} />
              {connected ? '已连接' : '轮询中'}
            </span>
            <span>{now.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</span>
          </div>
          <div className="mini-health">
            <div>
              <b>{d.sources.length ? `${healthy} / ${d.sources.length}` : DASH}</b>
              <span>数据源健康</span>
            </div>
            <div>
              <b>{tasksSeen ? `${tasksOk} / ${tasksSeen}` : DASH}</b>
              <span>任务正常</span>
            </div>
          </div>
        </div>
      </aside>

      <section className="shell">
        <div className="ticker">
          {d.market?.breadth ? (
            <>
              <span className="market">上涨 <b className="up">{d.market.breadth.advances ?? DASH}</b></span>
              <span className="market">下跌 <b className="down">{d.market.breadth.declines ?? DASH}</b></span>
              <span className="market">涨停 <b className="up">{d.market.breadth.limit_up ?? DASH}</b></span>
              <span className="market">跌停 <b className="down">{d.market.breadth.limit_down ?? DASH}</b></span>
              <span className="market">情绪 <b>{d.market.breadth.sentiment || DASH}</b></span>
              <span className="soft">· 快照 {fmtClock(d.market.captured_at)}</span>
            </>
          ) : (
            <span className="soft">尚无市场快照 — 盘中监控运行后这里显示涨跌家数与涨停情况</span>
          )}
        </div>

        <header className="topbar">
          <div className="crumb">
            <b>{TITLES[view]}</b>
            <span className="session-pill"
                  style={session.live ? undefined : {
                    background: 'var(--surface-3)', color: 'var(--muted)',
                  }}>
              {session.live && <i className="dot" />}{session.text}
            </span>
          </div>
          <div className="actions">
            <button className="icon-btn" onClick={() => setDark((v) => !v)}
                    title="切换明暗模式">{dark ? '☀' : '☾'}</button>
            <button className="btn" onClick={d.reload}>刷新</button>
          </div>
        </header>

        <main className="content">
          {d.loading ? (
            <div className="card pad"><p className="empty-note">加载中…</p></div>
          ) : (
            <>
              {view === 'home' && <HomeView {...d} />}
              {view === 'realtime' && <RealtimeView {...d} />}
              {view === 'anomalies' && <AnomaliesView {...d} />}
              {view === 'themes' && <ThemesView {...d} />}
              {view === 'portfolio' && <PortfolioView {...d} />}
              {view === 'graph' && <GraphView {...d} />}
              {view === 'reports' && <ReportsView {...d} />}
              {view === 'memory' && <MemoryView {...d} />}
              {view === 'system' && <SystemView {...d} />}
            </>
          )}
          <div className="footer-note">
            AlphaAgents · 数据来自实际抓取与调度产出，缺失一律显示 {DASH}
          </div>
        </main>
      </section>
    </div>
  )
}
