import { useEffect, useMemo, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Clock, FileText } from 'lucide-react'

function fmtTime(r) {
  const raw = r.created_at || (r.timestamp ? r.timestamp * 1000 : null)
  if (!raw) return ''
  const d = new Date(typeof raw === 'number' ? raw : raw.replace(' ', 'T') + 'Z')
  if (Number.isNaN(d.getTime())) return String(raw)
  return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

/** Reports carry no explicit kind, so label them by the hour they landed —
 *  the schedule is fixed (06:30 晨扫 / 盘中 / 15:30 复盘 / 20:00 夜扫). */
function kindOf(r) {
  const raw = r.created_at || (r.timestamp ? r.timestamp * 1000 : null)
  if (!raw) return { label: '报告', cls: 'badge-slate' }
  const d = new Date(typeof raw === 'number' ? raw : raw.replace(' ', 'T') + 'Z')
  const h = d.getHours()
  if (h < 9) return { label: '晨报', cls: 'badge-blue' }
  if (h < 15) return { label: '盘中', cls: 'badge-orange' }
  if (h < 19) return { label: '复盘', cls: 'badge-green' }
  return { label: '夜报', cls: 'badge-purple' }
}

export default function ReportPanel({ reports }) {
  const sorted = useMemo(
    () => [...reports].sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0) || (b.id || 0) - (a.id || 0)),
    [reports],
  )
  const [selected, setSelected] = useState(0)

  useEffect(() => { setSelected(0) }, [reports.length])

  if (!sorted.length) {
    return (
      <div className="card text-center py-12">
        <FileText className="w-8 h-8 mx-auto mb-3 text-slate-300 dark:text-slate-600" />
        <p className="text-sm text-slate-500 dark:text-slate-400">暂无报告</p>
        <p className="text-xs text-slate-400 dark:text-slate-500 mt-1">
          调度器会在 06:30 晨扫、15:30 复盘后写入
        </p>
      </div>
    )
  }

  const active = sorted[Math.min(selected, sorted.length - 1)]

  return (
    <div className="grid lg:grid-cols-[240px_1fr] gap-3">
      <div className="card !p-2 max-h-[70vh] overflow-y-auto">
        {sorted.map((r, i) => {
          const k = kindOf(r)
          const on = i === Math.min(selected, sorted.length - 1)
          return (
            <button
              key={r.id ?? i}
              onClick={() => setSelected(i)}
              className={`w-full text-left px-3 py-2.5 rounded-lg transition-colors mb-1
                ${on ? 'bg-blue-50 dark:bg-blue-500/15' : 'hover:bg-slate-50 dark:hover:bg-slate-700/50'}`}
            >
              <div className="flex items-center gap-2 mb-1">
                <span className={k.cls}>{k.label}</span>
                {r.event_count > 0 && (
                  <span className="text-xs text-slate-400">{r.event_count} 事件</span>
                )}
              </div>
              <div className="flex items-center gap-1 text-xs text-slate-500 dark:text-slate-400">
                <Clock className="w-3 h-3" />
                {fmtTime(r)}
              </div>
            </button>
          )
        })}
      </div>

      <div className="card max-h-[70vh] overflow-y-auto animate-in">
        <div className="flex items-center gap-2 mb-3 pb-3 border-b border-slate-200 dark:border-slate-700">
          <span className={kindOf(active).cls}>{kindOf(active).label}</span>
          <span className="text-xs text-slate-500 dark:text-slate-400">{fmtTime(active)}</span>
        </div>
        <div className="report-prose">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {active.report_text || active.text || '（报告内容为空）'}
          </ReactMarkdown>
        </div>
      </div>
    </div>
  )
}
