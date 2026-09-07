import { AlertTriangle, CheckCircle2, Loader2, Radio } from 'lucide-react'

const KIND_META = {
  task_start:  { icon: Loader2,        cls: 'badge-blue',   dot: 'status-dot-blue',   label: '运行中' },
  task_done:   { icon: CheckCircle2,   cls: 'badge-green',  dot: 'status-dot-green',  label: '完成' },
  task_failed: { icon: AlertTriangle,  cls: 'badge-red',    dot: 'status-dot-orange', label: '失败' },
}

function fmt(ts) {
  if (!ts) return ''
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export default function ActivityStream({ activity }) {
  if (!activity.length) {
    return (
      <div className="card text-center py-10">
        <Radio className="w-7 h-7 mx-auto mb-3 text-slate-300 dark:text-slate-600" />
        <p className="text-sm text-slate-500 dark:text-slate-400">暂无调度活动</p>
        <p className="text-xs text-slate-400 dark:text-slate-500 mt-1">
          调度器运行任务时会实时出现在这里
        </p>
      </div>
    )
  }

  return (
    <div className="card !p-0 overflow-hidden">
      <div className="px-4 py-3 border-b border-slate-200 dark:border-slate-700
                      flex items-center gap-2">
        <span className="status-dot-green" />
        <h3 className="text-sm font-semibold">实时消息流</h3>
        <span className="text-xs text-slate-400 ml-auto">{activity.length} 条</span>
      </div>

      <div className="max-h-[60vh] overflow-y-auto divide-y divide-slate-100 dark:divide-slate-700/60">
        {activity.map((a) => {
          const meta = KIND_META[a.kind] || KIND_META.task_done
          const Icon = meta.icon
          const secs = a.detail?.seconds
          return (
            <div key={a.id} className="px-4 py-2.5 flex gap-3 animate-in
                                       hover:bg-slate-50 dark:hover:bg-slate-700/30">
              <Icon className={`w-4 h-4 mt-0.5 shrink-0 ${
                a.kind === 'task_start' ? 'animate-spin text-blue-500'
                : a.kind === 'task_failed' ? 'text-red-500' : 'text-green-500'}`} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-xs font-medium text-slate-700 dark:text-slate-200">
                    {a.task || '任务'}
                  </span>
                  <span className={meta.cls}>{meta.label}</span>
                  {secs != null && (
                    <span className="text-xs text-slate-400 tabular-nums">{secs}s</span>
                  )}
                  <span className="text-xs text-slate-400 tabular-nums ml-auto">
                    {fmt(a.ts)}
                  </span>
                </div>
                {a.message && (
                  <p className="mt-1 text-xs text-slate-600 dark:text-slate-400
                                whitespace-pre-wrap line-clamp-6">
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
