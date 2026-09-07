import { CheckCircle2, FileText, Radio, Target } from 'lucide-react'

function Stat({ icon: Icon, label, value, sub, tone = 'blue' }) {
  const toneRing = {
    blue: 'bg-blue-50 text-blue-600 dark:bg-blue-500/15 dark:text-blue-400',
    green: 'bg-green-50 text-green-600 dark:bg-green-500/15 dark:text-green-400',
    purple: 'bg-purple-50 text-purple-600 dark:bg-purple-500/15 dark:text-purple-400',
    orange: 'bg-orange-50 text-orange-600 dark:bg-orange-500/15 dark:text-orange-400',
  }[tone]

  return (
    <div className="card flex items-center gap-3">
      <div className={`w-10 h-10 rounded-lg grid place-items-center shrink-0 ${toneRing}`}>
        <Icon className="w-5 h-5" />
      </div>
      <div className="min-w-0">
        <div className="text-xs text-slate-500 dark:text-slate-400">{label}</div>
        <div className="text-xl font-semibold tabular-nums leading-tight">{value}</div>
        {sub && <div className="text-xs text-slate-400 dark:text-slate-500 truncate">{sub}</div>}
      </div>
    </div>
  )
}

export default function StatCards({ summary, reportCount, sourceStats }) {
  const acc = summary.accuracy
  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
      <Stat
        icon={Target}
        tone={acc == null ? 'blue' : acc >= 0.5 ? 'green' : 'orange'}
        label="预测命中率"
        value={acc == null ? '—' : `${(acc * 100).toFixed(0)}%`}
        sub={summary.total ? `${summary.hit}/${summary.total} 条` : '尚无复盘数据'}
      />
      <Stat
        icon={CheckCircle2}
        tone="purple"
        label="已复盘天数"
        value={summary.days || '—'}
        sub={summary.days ? '最近若干交易日' : '等待 15:30 复盘'}
      />
      <Stat
        icon={FileText}
        tone="blue"
        label="分析报告"
        value={reportCount || '—'}
        sub={reportCount ? '晨扫 / 盘中 / 复盘' : '等待首轮生成'}
      />
      <Stat
        icon={Radio}
        tone={sourceStats.down > 0 ? 'orange' : 'green'}
        label="数据源"
        value={sourceStats.total ? `${sourceStats.up}/${sourceStats.total}` : '—'}
        sub={sourceStats.down > 0 ? `${sourceStats.down} 个异常` : '全部正常'}
      />
    </div>
  )
}
