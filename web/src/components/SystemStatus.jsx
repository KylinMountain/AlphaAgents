import { AlertTriangle, CheckCircle2, Clock } from 'lucide-react'

/* Scheduler activity used to be the dashboard's main panel: three rows
   every five minutes, 80 rows deep, all of it cron plumbing. It is status,
   not content — so it collapses to one line per task, newest run only. */

const STATUS_META = {
  ok: { icon: CheckCircle2, cls: 'text-green-500' },
  running: { icon: Clock, cls: 'text-blue-500' },
  degraded: { icon: AlertTriangle, cls: 'text-amber-500' },
  failed: { icon: AlertTriangle, cls: 'text-red-500' },
  timeout: { icon: AlertTriangle, cls: 'text-red-500' },
}

function fmtAge(ts) {
  if (!ts) return ''
  const then = new Date(ts).getTime()
  if (Number.isNaN(then)) return ''
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000))
  if (mins < 1) return '刚刚'
  if (mins < 60) return `${mins} 分钟前`
  const hrs = Math.round(mins / 60)
  return hrs < 24 ? `${hrs} 小时前` : `${Math.round(hrs / 24)} 天前`
}

export default function SystemStatus({ activity }) {
  // Rows arrive newest first, so the first sighting of a task is its
  // latest run.
  const latest = []
  const seen = new Set()
  for (const a of activity) {
    if (!a.task || seen.has(a.task)) continue
    seen.add(a.task)
    latest.push(a)
  }

  if (!latest.length) {
    return (
      <div className="card">
        <h3 className="text-sm font-semibold mb-2">系统状态</h3>
        <p className="text-xs text-slate-400">暂无调度记录</p>
      </div>
    )
  }

  return (
    <div className="card">
      <h3 className="text-sm font-semibold mb-2.5">系统状态</h3>
      <div className="space-y-2">
        {latest.map((a) => {
          const meta = STATUS_META[a.status] || STATUS_META.ok
          const Icon = meta.icon
          const degraded = a.status === 'degraded' || a.status === 'failed' ||
            a.status === 'timeout'
          return (
            <div key={a.id} className="flex items-start gap-2">
              <Icon className={`w-3.5 h-3.5 mt-0.5 shrink-0 ${meta.cls}`} />
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline gap-2">
                  <span className="text-xs text-slate-700 dark:text-slate-200 truncate">
                    {a.task}
                  </span>
                  <span className="text-[11px] text-slate-400 ml-auto shrink-0">
                    {fmtAge(a.ts)}
                  </span>
                </div>
                {degraded && a.message && (
                  <p className="text-[11px] text-amber-600 dark:text-amber-400
                                line-clamp-2 leading-snug">
                    {a.message}
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
