import { DASH, fmtAge } from '../lib/format'

/* Scheduler and source observability.
 *
 * The failure this page exists to make visible: the pipeline appearing to
 * run while its output is an error string, or a task crashing every cycle
 * with nothing on screen to say so. */

const TASK_NOTE = {
  news_ingest: '每 5 分钟摄取全部新闻源',
  morning_scan: '09:00 晨扫',
  opening_reminder: '09:25 开盘提醒',
  intraday_monitor: '09:30–15:00 每 5 分钟盘中追因',
  review: '15:30 复盘并验证预测',
  night_scan: '20:00 夜扫',
  weekly_report: '周六 10:00 周报',
}

function iconOf(status) {
  if (status === 'ok') return 'ok'
  if (status === 'degraded' || status === 'running') return 'warn'
  return 'bad'
}

export default function SystemView({ activity, sources, failed }) {
  const seen = new Set()
  const latest = []
  for (const a of activity) {
    if (!a.task || seen.has(a.task)) continue
    seen.add(a.task)
    latest.push(a)
  }

  const healthy = sources.filter((s) => s.healthy)
  const down = sources.filter((s) => !s.healthy)

  return (
    <section className="view active">
      <div className="page-head">
        <div>
          <h1>系统状态</h1>
          <p>调度器、Agent 与数据源都必须可观测，避免"看起来在工作但其实数据断了"。</p>
        </div>
        <div className="date-note">
          <b>{latest.length} 个任务有记录</b>
          {sources.length ? `${healthy.length} / ${sources.length} 数据源健康` : DASH}
        </div>
      </div>

      {failed.length > 0 && (
        <div className="card pad" style={{ marginBottom: 14, borderColor: 'var(--red)' }}>
          <div className="card-title"><h3 style={{ color: 'var(--red)' }}>接口请求失败</h3></div>
          <p className="empty-note">
            以下接口本轮轮询失败，对应面板显示的是上一次成功的数据：{failed.join('、')}
          </p>
        </div>
      )}

      <div className="grid g12">
        <div className="card pad" style={{ gridColumn: 'span 6' }}>
          <div className="card-title">
            <h3>调度任务</h3>
            <span>{latest.filter((a) => a.status === 'ok').length} / {latest.length} 正常</span>
          </div>
          {latest.length === 0 ? (
            <p className="empty-note">
              暂无调度记录。调度器容器停止时不会写入任何活动。
            </p>
          ) : latest.map((a) => (
            <div className="status-item" key={a.id}>
              <i className={`status-icon ${iconOf(a.status)}`} />
              <div>
                <b>{a.task}</b>
                <span>{a.status === 'ok'
                  ? (TASK_NOTE[a.task] || '运行正常')
                  : (a.message || a.status)}</span>
              </div>
              <span className="status-time">{fmtAge(a.ts)}</span>
            </div>
          ))}
        </div>

        <div className="card pad" style={{ gridColumn: 'span 6' }}>
          <div className="card-title">
            <h3>数据源</h3>
            <span>{sources.length ? `${healthy.length} / ${sources.length}` : DASH}</span>
          </div>
          {sources.length === 0 ? (
            <p className="empty-note">尚无数据源健康记录。</p>
          ) : (
            <>
              {down.length > 0 && (
                <div className="health-group">
                  <h4>异常</h4>
                  {down.map((s) => (
                    <div className="health-source" key={s.id}>
                      <span className="name">
                        <i className="tiny-dot" style={{ background: 'var(--red)' }} />
                        {s.name}
                      </span>
                      <span>{(s.last_error || '').slice(0, 40) || '不健康'}</span>
                    </div>
                  ))}
                </div>
              )}
              <div className="health-group">
                <h4>正常 ({healthy.length})</h4>
                {healthy.map((s) => (
                  <div className="health-source" key={s.id}>
                    <span className="name"><i className="tiny-dot" />{s.name}</span>
                    <span>{s.total_items || 0} 条</span>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </div>
    </section>
  )
}
