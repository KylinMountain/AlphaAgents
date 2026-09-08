import { DASH, fmtDateTime, stageOf } from '../lib/format'

/* Theme lifecycle table. Columns are exactly the theme_lines fields —
 * the prototype's 3日资金 / 催化强度 columns have no backing store, and a
 * column that is always a dash is worse than no column. */

export default function ThemesView({ themes }) {
  return (
    <section className="view active">
      <div className="page-head">
        <div>
          <h1>投资主线</h1>
          <p>用生命周期管理市场叙事：观察、活跃、主升、衰退、归档。由复盘与夜扫维护。</p>
        </div>
        <div className="date-note"><b>{themes.length} 条活跃主线</b>已归档的不在此列</div>
      </div>

      {themes.length === 0 ? (
        <div className="card pad">
          <p className="empty-note">
            尚无主线。主线由 15:30 复盘和 20:00 夜扫创建、加强或降级，
            调度器停止时不会产生新记录。
          </p>
        </div>
      ) : (
        <div className="card table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>主线</th><th>阶段</th><th>强度</th><th>龙头</th>
                <th>核心标的</th><th>催化</th><th>更新时间</th>
              </tr>
            </thead>
            <tbody>
              {themes.map((t) => {
                const stage = stageOf(t.status)
                return (
                  <tr key={t.id ?? t.name}>
                    <td><b>{t.name}</b></td>
                    <td><span className={`stage-chip ${stage.cls}`}>{stage.label}</span></td>
                    <td className={t.strength >= 7 ? 'up' : t.strength <= 3 ? 'down' : ''}>
                      {t.strength ?? DASH}
                    </td>
                    <td>{t.leader_code || DASH}</td>
                    <td>{t.core_stocks || DASH}</td>
                    <td>{t.catalyst || DASH}</td>
                    <td>{fmtDateTime(t.updated_at)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
