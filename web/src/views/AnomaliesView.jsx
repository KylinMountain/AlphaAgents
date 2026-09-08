import { useMemo, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  DASH, fmtClock, fmtDateTime, fmtPct, reportToMarkdown, trendClass,
} from '../lib/format'

/* 盘中追因 timeline.
 *
 * The prototype invents a "持续性 91%" figure per row. The pipeline does not
 * produce one — it produces a pick with a confidence label, a theme and a
 * reason — so those are what this shows. Inventing the percentage would be
 * the one thing a validation-focused dashboard must not do. */

export default function AnomaliesView({ signals, reports, portfolio }) {
  // The picks were the only thing this page could show, so it answered
  // "which stocks" without ever answering "why" — the agent's actual
  // attribution (fund flow, limit-up structure, style rotation) is a
  // multi-thousand-character report that only reached the notification.
  const [openReport, setOpenReport] = useState(false)
  const attribution = useMemo(() => {
    const rows = (reports || [])
      .filter((r) => r.report_type === 'intraday_monitor' && r.report_text)
      .sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0))
    return rows[0] || null
  }, [reports])

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
        <div className="card anomaly-stat"><span>涨停确认</span>{/* a price fact: A-share red */}
          <b className="up">{confirmed}</b></div>
        <div className="card anomaly-stat"><span>可操作标的</span><b>{actionable}</b></div>
        <div className="card anomaly-stat">
          <span>覆盖主线</span>
          <b>{new Set(signals.map((s) => s.theme_line || s.theme).filter(Boolean)).size}</b>
        </div>
      </div>

      {attribution ? (
        <article className="card pad" style={{ marginBottom: 14 }}>
          <div className="card-title">
            <h3>本轮追因</h3>
            <span>
              {fmtDateTime(attribution.timestamp
                ? attribution.timestamp * 1000 : attribution.created_at)}
            </span>
          </div>
          <div className="report-prose" style={{
            maxHeight: openReport ? 'none' : 220, overflow: 'hidden',
            position: 'relative',
          }}>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {reportToMarkdown(attribution.report_text)}
            </ReactMarkdown>
          </div>
          <button className="filter" style={{ marginTop: 8 }}
                  onClick={() => setOpenReport((v) => !v)}>
            {openReport ? '收起追因' : '展开完整追因'}
          </button>
        </article>
      ) : signals.length > 0 && (
        <article className="card pad" style={{ marginBottom: 14 }}>
          <p className="empty-note">
            下面的标的来自主线内的量化筛选。完整的异动追因（资金流、涨停结构、
            风格轮动）由调度器运行 intraday_monitor 时写入报告表，手工触发的单次
            运行不会留下这份记录。
          </p>
        </article>
      )}

      {(portfolio?.pending || []).length > 0 && (
        <article className="card table-wrap" style={{ marginBottom: 14 }}>
          <div className="card-title" style={{ padding: '14px 14px 0' }}>
            <h3>虚拟挂单</h3>
            <span>
              {portfolio.pending.length} 笔待成交
              {portfolio.positions?.length ? ` · ${portfolio.positions.length} 笔持仓` : ''}
            </span>
          </div>
          <table className="table">
            <thead>
              <tr><th>标的</th><th>主线</th><th>入场区间</th><th>止损</th><th>下单日</th></tr>
            </thead>
            <tbody>
              {portfolio.pending.map((o) => (
                <tr key={o.id}>
                  <td><b>{o.name}</b> · {o.code}</td>
                  <td>{o.theme || DASH}</td>
                  <td className="tabular">
                    {o.entry_low != null && o.entry_high != null
                      ? `${o.entry_low} – ${o.entry_high}` : DASH}
                  </td>
                  <td className="down">{o.stop_loss ?? DASH}</td>
                  <td>{o.order_date || DASH}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </article>
      )}

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
                  the date. Rendered as MM-DD it fits the column; the full
                  ISO date wrapped onto two lines. */}
              {s.created_at
                ? fmtClock(s.created_at)
                : (s.date ? String(s.date).slice(5) : DASH)}
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
