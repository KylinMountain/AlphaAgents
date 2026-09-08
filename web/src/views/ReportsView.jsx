import { useMemo, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { DASH, fmtDateTime, parseStamp, reportToMarkdown } from '../lib/format'

/* Reports list + reader.
 *
 * A report whose body is an error string ("[stock agent error: …]") is shown
 * as failed rather than rendered as analysis — that state was invisible for
 * a full day of dead-model output, which is exactly when it mattered most. */

/* report_type records which task produced the report. Rows written before
   that column existed have none, so the hour is the fallback — a guess,
   and one that was wrong for anything that ran late. */
const TASK_LABEL = {
  morning_scan: { label: '晨报', cls: 'stage-active' },
  opening_reminder: { label: '开盘', cls: 'stage-sprout' },
  intraday_monitor: { label: '盘中', cls: 'stage-sprout' },
  review: { label: '复盘', cls: 'stage-main' },
  night_scan: { label: '夜报', cls: 'stage-fade' },
  weekly_report: { label: '周报', cls: 'stage-main' },
}

function kindOf(r) {
  if (r.report_type && TASK_LABEL[r.report_type]) return TASK_LABEL[r.report_type]
  if (r.cycle != null) return { label: '追因', cls: 'stage-sprout' }

  const d = parseStamp(stampOf(r))
  if (!d) return { label: '报告', cls: 'stage-active' }
  const h = d.getHours()
  if (h < 9) return { label: '晨报', cls: 'stage-active' }
  if (h < 15) return { label: '盘中', cls: 'stage-sprout' }
  if (h < 19) return { label: '复盘', cls: 'stage-main' }
  return { label: '夜报', cls: 'stage-fade' }
}

/** Prefer the epoch column: created_at came from SQLite's datetime('now'),
 *  which is UTC, and rendered eight hours before the timestamp printed in
 *  the report's own body. New rows are written local, old ones are not. */
function stampOf(r) {
  return r.timestamp ? r.timestamp * 1000 : r.created_at
}

function isFailed(text) {
  return typeof text === 'string' && text.trimStart().startsWith('[')
}

export default function ReportsView({ reports }) {
  const sorted = useMemo(
    () => [...reports].sort(
      (a, b) => (b.timestamp || 0) - (a.timestamp || 0) || (b.id || 0) - (a.id || 0),
    ),
    [reports],
  )
  // Selection is held by report id, not list index: a new report arriving
  // shifts every index by one, so an index would silently start pointing
  // at a different report on each poll. An id that has scrolled out of the
  // list falls back to the newest.
  const [selectedId, setSelectedId] = useState(null)

  if (!sorted.length) {
    return (
      <section className="view active">
        <div className="page-head"><div><h1>分析报告</h1>
          <p>晨报、盘中提醒、复盘、夜报和周报。</p></div></div>
        <div className="card pad">
          <p className="empty-note">
            尚无报告。调度器会在 09:00 晨扫、盘中追因、15:30 复盘、20:00 夜扫后写入。
          </p>
        </div>
      </section>
    )
  }

  const active = sorted.find((r) => r.id === selectedId) || sorted[0]
  const body = active.report_text || active.text || ''
  const failed = isFailed(body)

  return (
    <section className="view active">
      <div className="page-head">
        <div><h1>分析报告</h1><p>结论优先、证据展开。失败的报告会标红，不会伪装成分析。</p></div>
        <div className="date-note">
          <b>{sorted.length} 篇</b>
          {sorted.filter((r) => isFailed(r.report_text)).length} 篇生成失败
        </div>
      </div>

      <div className="report-grid">
        <article className="card report-body">
          <div className="section-kicker">
            {kindOf(active).label} · {fmtDateTime(stampOf(active))}
            {active.event_count ? ` · ${active.event_count} 个事件` : ''}
          </div>
          {failed ? (
            <>
              <h2 style={{ color: 'var(--red)' }}>这篇报告生成失败</h2>
              <div className="risk-note" style={{
                background: 'var(--red-soft)', color: 'var(--red)',
                whiteSpace: 'pre-wrap', fontFamily: 'ui-monospace, monospace',
              }}>
                {body.slice(0, 800)}
              </div>
            </>
          ) : (
            <div className="report-prose">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {reportToMarkdown(body)}
              </ReactMarkdown>
            </div>
          )}
        </article>

        <aside className="card report-nav">
          <div className="card-title"><h3>报告时间线</h3><span>最近 {sorted.length} 篇</span></div>
          {sorted.map((r, i) => {
            const k = kindOf(r)
            const bad = isFailed(r.report_text)
            return (
              <button key={r.id ?? i}
                      onClick={() => setSelectedId(r.id ?? null)}
                      className={`item ${r === active ? 'active' : ''}`}
                      style={{
                        display: 'flex', width: '100%', border: 0,
                        background: r === active ? 'var(--blue-soft)' : 'transparent',
                        cursor: 'pointer', textAlign: 'left',
                      }}>
                <span>
                  <span className={`stage-chip ${k.cls}`} style={{ marginRight: 6 }}>
                    {k.label}
                  </span>
                  {fmtDateTime(stampOf(r))}
                </span>
                <span style={bad ? { color: 'var(--red)' } : undefined}>
                  {bad ? '失败' : (r.event_count ? `${r.event_count} 事件` : DASH)}
                </span>
              </button>
            )
          })}
        </aside>
      </div>
    </section>
  )
}
