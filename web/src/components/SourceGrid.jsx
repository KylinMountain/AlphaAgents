import { Globe, Landmark, MessageCircle } from 'lucide-react'

const TYPE_META = {
  domestic: { label: '国内', icon: Landmark },
  international: { label: '海外', icon: Globe },
  social: { label: '社交', icon: MessageCircle },
}

export default function SourceGrid({ sources }) {
  if (!sources.length) {
    return (
      <div className="card">
        <h3 className="text-sm font-semibold mb-2">数据源</h3>
        <p className="text-xs text-slate-500 dark:text-slate-400">暂无健康数据</p>
      </div>
    )
  }

  const groups = sources.reduce((acc, s) => {
    ;(acc[s.type] ||= []).push(s)
    return acc
  }, {})

  return (
    <div className="card">
      <h3 className="text-sm font-semibold mb-3">数据源健康</h3>
      <div className="space-y-4">
        {Object.entries(groups).map(([type, list]) => {
          const meta = TYPE_META[type] || { label: type, icon: Globe }
          const Icon = meta.icon
          return (
            <div key={type}>
              <div className="flex items-center gap-1.5 mb-2 text-xs font-medium
                              text-slate-500 dark:text-slate-400">
                <Icon className="w-3.5 h-3.5" />
                {meta.label}
              </div>
              <div className="space-y-1.5">
                {list.map((s) => (
                  <div key={s.id} className="flex items-center gap-2 text-xs">
                    <span className={s.healthy ? 'status-dot-green' : 'status-dot-orange'} />
                    <span className="truncate flex-1 text-slate-700 dark:text-slate-300">
                      {s.name}
                    </span>
                    {s.total_items > 0 && (
                      <span className="tabular-nums text-slate-400 dark:text-slate-500">
                        {s.total_items}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
