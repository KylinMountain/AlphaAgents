import { useMemo, useState } from 'react'
import { ExternalLink, Radio } from 'lucide-react'

/* Colour is per source rather than per category so the eye can filter one
   outlet out of a fast-scrolling list without reading any text. */
const SOURCE_STYLE = {
  财联社电报: 'bg-red-50 text-red-600 dark:bg-red-500/10 dark:text-red-300',
  新浪7x24: 'bg-blue-50 text-blue-600 dark:bg-blue-500/10 dark:text-blue-300',
  金十数据: 'bg-amber-50 text-amber-600 dark:bg-amber-500/10 dark:text-amber-300',
  东方财富7x24: 'bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-300',
  华尔街见闻: 'bg-violet-50 text-violet-600 dark:bg-violet-500/10 dark:text-violet-300',
}
const DEFAULT_STYLE =
  'bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300'

function fmtTime(ts) {
  if (!ts) return ''
  const m = /(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(ts)
  if (!m) return ts
  const [, y, mo, d, hh, mm] = m
  const today = new Date()
  const isToday =
    Number(y) === today.getFullYear() &&
    Number(mo) === today.getMonth() + 1 &&
    Number(d) === today.getDate()
  return isToday ? `${hh}:${mm}` : `${mo}-${d} ${hh}:${mm}`
}

/* The flash body usually repeats the headline verbatim — sources put the
   same sentence in both fields — so showing both doubles the row height
   for nothing. */
function bodyOf(item) {
  const s = (item.summary || '').trim()
  const t = (item.title || '').trim()
  if (!s || s === t) return ''
  return s.startsWith(t) ? s.slice(t.length).trim() : s
}

export default function NewsStream({ news }) {
  const [active, setActive] = useState('全部')

  const sources = useMemo(() => {
    const counts = new Map()
    news.forEach((n) => counts.set(n.source, (counts.get(n.source) || 0) + 1))
    return [...counts.entries()].sort((a, b) => b[1] - a[1])
  }, [news])

  const shown = active === '全部' ? news : news.filter((n) => n.source === active)

  if (!news.length) {
    return (
      <div className="card text-center py-10">
        <Radio className="w-7 h-7 mx-auto mb-3 text-slate-300 dark:text-slate-600" />
        <p className="text-sm text-slate-500 dark:text-slate-400">暂无快讯</p>
        <p className="text-xs text-slate-400 dark:text-slate-500 mt-1">
          新闻摄取每 5 分钟运行一次，稍候刷新
        </p>
      </div>
    )
  }

  return (
    <div className="card !p-0 overflow-hidden">
      <div className="px-4 py-3 border-b border-slate-200 dark:border-slate-700
                      flex items-center gap-2">
        <span className="status-dot-green" />
        <h3 className="text-sm font-semibold">实时快讯</h3>
        <span className="text-xs text-slate-400 ml-auto">{shown.length} 条</span>
      </div>

      <div className="px-3 py-2 flex gap-1.5 overflow-x-auto border-b
                      border-slate-100 dark:border-slate-700/60">
        {[['全部', news.length], ...sources].map(([name, count]) => (
          <button
            key={name}
            onClick={() => setActive(name)}
            className={`px-2 py-1 rounded-md text-xs whitespace-nowrap transition-colors
              ${active === name
                ? 'bg-blue-500 text-white'
                : 'text-slate-500 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-700'}`}
          >
            {name}
            <span className="ml-1 opacity-70 tabular-nums">{count}</span>
          </button>
        ))}
      </div>

      <div className="max-h-[65vh] overflow-y-auto divide-y divide-slate-100
                      dark:divide-slate-700/60">
        {shown.map((n, i) => {
          const body = bodyOf(n)
          return (
            <div
              key={`${n.time}-${n.source}-${i}`}
              className="px-4 py-2.5 flex gap-3 hover:bg-slate-50 dark:hover:bg-slate-700/30"
            >
              <span className="text-xs text-slate-400 tabular-nums pt-0.5 shrink-0 w-11">
                {fmtTime(n.time)}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex items-start gap-2">
                  <span className={`px-1.5 py-0.5 rounded text-[11px] shrink-0
                                    ${SOURCE_STYLE[n.source] || DEFAULT_STYLE}`}>
                    {n.source}
                  </span>
                  <p className="text-sm text-slate-800 dark:text-slate-100 leading-snug">
                    {n.title}
                  </p>
                  {n.url && (
                    <a
                      href={n.url}
                      target="_blank"
                      rel="noreferrer"
                      className="shrink-0 text-slate-300 hover:text-blue-500 mt-0.5"
                      title="打开原文"
                    >
                      <ExternalLink className="w-3.5 h-3.5" />
                    </a>
                  )}
                </div>
                {body && (
                  <p className="mt-1 text-xs text-slate-500 dark:text-slate-400
                                leading-relaxed line-clamp-3">
                    {body}
                  </p>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
