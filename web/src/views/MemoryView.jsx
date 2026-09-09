import { DASH, fmtPct } from '../lib/format'

/* Prediction validation.
 *
 * The ring shows the hit rate only when there are scored predictions. With
 * none, it shows a dash — a 0% ring reads as "the system is wrong about
 * everything" when it actually means "nothing has been graded yet". */

/* The agent's own calibration, which is a different question from its hit
 * rate. Hit rate asks "is it right"; this asks "does it know how likely it
 * is to be right" — and that is learnable from far fewer samples, because
 * every closed thesis contributes a row, not only the profitable ones. */
function CalibrationCard({ calibration }) {
  const curve = calibration?.curve || []
  const blind = calibration?.blind_spots || {}
  const never = (calibration?.conditions || [])
    .filter((c) => c.written >= 5 && c.fired === 0)

  const empty = curve.length === 0 && !blind.n

  return (
    <article className="card pad" style={{ marginTop: 14 }}>
      <div className="card-title">
        <h3>自我校准</h3><span>近 60 日已结论点</span>
      </div>
      {empty ? (
        <p className="empty-note">
          还没有已结论点。每笔建仓会写下一个论点（声称、概率、期限、失效条件），
          平仓时判定它是按自己列出的条件失效、到期兑现，还是
          <b> 盲点</b>——被风控硬线平掉而一条列出的条件都没响。
          这里统计的就是那些判定。
        </p>
      ) : (
        <>
          {curve.length > 0 && (
            <div className="calib">
              {curve.map((row) => {
                const stated = row.bucket
                const actual = Math.round(row.hit_rate * 100)
                return (
                  <div className="calib-row" key={stated}>
                    <span className="calib-label">说 {stated}</span>
                    <span className="calib-bar">
                      <i style={{ width: `${Math.min(100, actual)}%` }} />
                    </span>
                    <span className="calib-val">实际 {actual}%</span>
                    <span className="calib-n">n={row.n}</span>
                  </div>
                )
              })}
            </div>
          )}
          {blind.n > 0 && (
            <p className="calib-note">
              <b>盲点率 {Math.round((blind.rate || 0) * 100)}%</b>
              （{blind.blind}/{blind.n} 笔亏损没有任何列出的条件触发就被平掉）
              {blind.unguarded ? `，另有 ${blind.unguarded} 条论点从头没写失效条件` : ''}
            </p>
          )}
          {never.length > 0 && (
            <p className="calib-note soft">
              从未触发的条件：{never.map((c) => c.kind).join('、')}
              —— 写了多次一次没响，阈值可能设在了价格不会去的地方。
            </p>
          )}
        </>
      )}
    </article>
  )
}

export default function MemoryView({ stats, reviews, calibration }) {
  const has = Boolean(stats?.total)
  const pct = has ? stats.hit_rate : null
  const byConf = Object.entries(stats?.by_confidence || {})

  return (
    <section className="view active">
      <div className="page-head">
        <div>
          <h1>记忆与验证</h1>
          <p>保存判断、证据、结果和错误归因。命中率只统计已评分的预测。</p>
        </div>
        <div className="date-note">
          <b>{has ? `${stats.hits} / ${stats.total} 命中` : '尚无评分'}</b>
          复盘任务负责评分
        </div>
      </div>

      <div className="memory-grid">
        <div className="card hit-card">
          <div className="section-kicker">近 7 日验证</div>
          <div className="ring" style={{
            background: has
              ? `conic-gradient(var(--blue) 0 ${pct}%, var(--surface-3) ${pct}%)`
              : 'var(--surface-3)',
          }}>
            <div>
              <b>{has ? `${pct}%` : DASH}</b>
              <span>{has ? `${stats.hits} / ${stats.total} 命中` : '暂无已评分预测'}</span>
            </div>
          </div>
          {byConf.length > 0 && (
            <div className="health-group" style={{ textAlign: 'left' }}>
              <h4>按信心分层</h4>
              {byConf.map(([conf, v]) => (
                <div className="health-source" key={conf}>
                  <span className="name">{conf}</span>
                  <span>{v.hits} / {v.total} · {v.hit_rate}%</span>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="card table-wrap">
          {reviews.length === 0 ? (
            <p className="empty-note" style={{ padding: 16 }}>
              尚无复盘记录。15:30 复盘会验证当日预测并写入这里。
            </p>
          ) : (
            <table className="table">
              <thead>
                <tr><th>日期</th><th>预测数</th><th>命中</th><th>命中率</th><th>摘要</th></tr>
              </thead>
              <tbody>
                {reviews.map((r) => {
                  const rate = r.predictions_count
                    ? (r.correct_count / r.predictions_count) * 100
                    : null
                  return (
                    <tr key={r.id ?? r.date}>
                      <td><b>{r.date}</b></td>
                      <td>{r.predictions_count ?? DASH}</td>
                      <td>{r.correct_count ?? DASH}</td>
                      {/* Deliberately not the up/down colours: those mean
                          "price rose / fell" and A-share red-for-up would
                          paint a good hit rate as a loss. */}
                      <td style={rate == null ? undefined : {
                        color: rate >= 50 ? 'var(--blue)' : 'var(--muted)',
                        fontWeight: 650,
                      }}>
                        {rate == null ? DASH : fmtPct(rate, 0).replace('+', '')}
                      </td>
                      <td>{(r.summary || r.review_text || '').slice(0, 60) || DASH}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <CalibrationCard calibration={calibration} />
    </section>
  )
}
