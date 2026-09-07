import { Activity, ExternalLink, Moon, RefreshCw, Sun } from 'lucide-react'

export default function Header({ connected, dark, onToggleDark, onRefresh, refreshing }) {
  return (
    <header className="sticky top-0 z-20 border-b border-slate-200 dark:border-slate-700
                       bg-white/80 dark:bg-slate-900/80 backdrop-blur">
      <div className="mx-auto max-w-7xl px-4 h-14 flex items-center gap-4">
        <div className="flex items-center gap-2 min-w-0">
          <Activity className="w-5 h-5 text-blue-500 shrink-0" />
          <span className="font-semibold text-base text-gradient whitespace-nowrap">
            AlphaAgents
          </span>
          <span className="hidden sm:inline text-xs text-slate-500 dark:text-slate-400 truncate">
            资金行为 × 新闻归因
          </span>
        </div>

        <div className="flex-1" />

        <div className="flex items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400">
          <span className={connected ? 'status-dot-green' : 'status-dot-slate'} />
          <span className="hidden sm:inline">{connected ? '已连接' : '离线'}</span>
        </div>

        <button
          onClick={onRefresh}
          className="btn-secondary !px-2 !py-1.5"
          title="刷新"
          aria-label="刷新"
        >
          <RefreshCw className={`w-4 h-4 ${refreshing ? 'animate-spin' : ''}`} />
        </button>

        <button
          onClick={onToggleDark}
          className="btn-secondary !px-2 !py-1.5"
          title={dark ? '切换到亮色' : '切换到暗色'}
          aria-label="切换主题"
        >
          {dark ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
        </button>

        <a
          href="https://github.com/KylinMountain/AlphaAgents"
          target="_blank"
          rel="noreferrer"
          className="btn-secondary !px-2 !py-1.5"
          title="GitHub"
          aria-label="GitHub"
        >
          <ExternalLink className="w-4 h-4" />
        </a>
      </div>
    </header>
  )
}
