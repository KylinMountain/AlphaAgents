import { DASH, fmtClock, fmtPct, trendClass } from '../lib/format'

/* 盘中追因 timeline.
 *
 * The prototype invents a "持续性 91%" figure per row. The pipeline does not
 * produce one — it produces a pick with a confidence label, a theme and a
 * reason — so those are what this shows. Inventing the percentage would be
 * the one thing a validation-focused dashboard must not do. */

export default function AnomaliesView({ signals }) {
  const confirmed = signals.filter((s) => s.confidence === 'signal').length
  const actionable = signals.length - confirmed

  return (
    <section className="view active">
      <div className="page-head">
        <div>
          <h1>实时异动</h1>
          <p>盘中监控 09:30–15:00 每 5 分钟检测一次，检测到异动才调用 agent 追因。</p>
        </div>
        <div className="date-note">
          <b><span className="live-dot" />今日 {signals.length} 条信号</b>
          由 intraday_monitor 写入
        </div>
      </div>

      <div className="anomaly-hero">
        <div className="card anomaly-stat"><span>今日信号</span><b>{signals.length}</b></div>
        <div className="card anomaly-stat"><span>涨停确认</span><b className="up">{confirmed}</b></div>
        <div className="card anomaly-stat"><span>可操作标的</span><b>{actionable}</b></div>
        <div className="card anomaly-stat">
          <span>覆盖主线</span>
          <b>{new Set(signals.map((s) => s.theme_line || s.theme).filter(Boolean)).size}</b>
        </div>
      </div>

      <div className="card timeline-card">
        <div className="timeline-head">
          <span>时间</span><span>类型</span><span>标的 / 追因</span><span>记录</span>
        </div>
        {signals.length === 0 ? (
          <p className="empty-note" style={{ padding: '16px' }}>
            今日尚无盘中信号。没有异动就不产出，这是设计如此，不是故障。
          </p>
        ) : signals.map((s) => (
          <div className="timeline-row" key={s.id ?? `${s.code}-${s.created_at}`}>
            <div className="timeline-time">
              {/* Rows saved before predictions gained created_at have only
                  the date; showing a dash there implied missing data when
                  the time simply was never recorded. */}
              {s.created_at ? fmtClock(s.created_at) : (s.date || DASH)}
            </div>
            <div>
              <span className={`severity ${s.confidence === 'signal' ? 'high' : 'mid'}`}>
                {s.confidence === 'signal' ? '涨停确认' : s.confidence || '观察'}
              </span>
            </div>
            <div className="timeline-content">
              <b>{s.name || s.code} · {s.code}</b>
              <p>{s.reason
                || 'agent 未给出理由 — 该标的来自主线内的量化筛选（beta / 涨幅 / 流动性），不是新闻归因'}</p>
              <div className="cause-chain">
                {(s.theme_line || s.theme) && <span>{s.theme_line || s.theme}</span>}
                {s.direction && <><i className="arrow">→</i><span>{s.direction}</span></>}
                {s.report_type && <><i className="arrow">→</i><span>{s.report_type}</span></>}
              </div>
            </div>
            <div className="timeline-result">
              <strong className={trendClass(s.next_day_return)}>
                {s.next_day_return != null ? fmtPct(s.next_day_return) : '待验证'}
              </strong>
              <span>{s.entry_price != null ? `记录价 ${s.entry_price}` : DASH}</span>
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}
