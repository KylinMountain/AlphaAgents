import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { CalendarCheck } from 'lucide-react'

function accBadge(a) {
  if (a == null) return 'badge-slate'
  if (a >= 0.6) return 'badge-green'
  if (a >= 0.4) return 'badge-orange'
  return 'badge-red'
}

export default function ReviewPanel({ reviews }) {
  const [open, setOpen] = useState(0)

  if (!reviews.length) {
    return (
      <div className="card text-center py-12">
        <CalendarCheck className="w-8 h-8 mx-auto mb-3 text-slate-300 dark:text-slate-600" />
        <p className="text-sm text-slate-500 dark:text-slate-400">暂无复盘</p>
        <p className="text-xs text-slate-400 dark:text-slate-500 mt-1">每交易日 15:30 生成</p>
      </div>
    )
  }

  return (
    <div className="space-y-3">
      {reviews.map((r, i) => {
        const acc = r.accuracy ?? (r.predictions_count ? r.correct_count / r.predictions_count : null)
        const isOpen = i === open
        return (
          <div key={r.id ?? r.date} className="card animate-in">
            <button
              className="w-full flex items-center gap-3 text-left"
              onClick={() => setOpen(isOpen ? -1 : i)}
            >
              <span className="font-semibold tabular-nums">{r.date}</span>
              <span className={accBadge(acc)}>
                {acc == null ? '—' : `命中 ${(acc * 100).toFixed(0)}%`}
              </span>
              <span className="text-xs text-slate-500 dark:text-slate-400">
                {r.correct_count ?? 0}/{r.predictions_count ?? 0} 条预测
              </span>
              <span className="flex-1" />
              <span className="text-xs text-slate-400">{isOpen ? '收起' : '展开'}</span>
            </button>

            {isOpen && r.review_text && (
              <div className="report-prose mt-3 pt-3 border-t border-slate-200 dark:border-slate-700">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{r.review_text}</ReactMarkdown>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
