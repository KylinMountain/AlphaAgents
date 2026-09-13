import { DASH, fmtPct } from '../lib/format'
import { WorkspaceCard, WorkspaceHead } from '../components/WorkspaceSection'

/* The learn journal: what was decided, what it turned into, what it taught.
 *
 * §9 keeps three outcomes apart on purpose — a decision can be a correct
 * forecast and a losing trade at the same time — so this page never merges
 * them into one "performance" number. The four cards below are the four
 * questions, and each one states the table it read.
 */

const STATE_LABEL = {
  pending: '待定',
  matured: '已成熟',
  censored: '已截断',
  revised: '已修订',
}

const KIND_LABEL = {
  forecast: '预测',
  trade: '交易',
  process: '过程',
}

const CANDIDATE_LABEL = {
  observation: '观察',
  hypothesis: '假设',
  testing: '在测',
  validated: '已验证',
  retired: '已退役',
}

function Kpi({ label, value, valueClass = '', note }) {
  return (
    <div className="kpi">
      <div className="kpi-head"><span>{label}</span></div>
      <b className={valueClass}>{value}</b>
      <small>{note}</small>
    </div>
  )
}

function Row({ label, value }) {
  return (
    <tr>
      <td>{label}</td>
      <td className="num">{value ?? DASH}</td>
    </tr>
  )
}

function Integrity({ items }) {
  if (!items?.length) return null
  return (
    <div className="ws-integrity">
      <b>结构性问题 {items.length} 条</b>
      <ul>{(items || []).map((i, n) => <li key={n}>{i}</li>)}</ul>
    </div>
  )
}

/* ── 决策覆盖：§9 的 selection and coverage ───────────────────────── */
function Episodes({ sec }) {
  const v = sec?.value || {}
  const c = v.coverage || {}
  return (
    <WorkspaceCard title="决策覆盖" sec={sec}
                   subtitle="「我们决策了 N 次、成交 M 次」——这个问题在持仓表里答不出来">
      <table className="table">
        <tbody>
          <Row label="决策数（episode 数）" value={c.decisions} />
          <Row label="有终局裁决" value={c.verdicts} />
          <Row label="进程死在中途（已问未答）" value={c.no_verdict} />
          <Row label="被拒绝" value={c.refused} />
          <Row label="成交" value={c.traded} />
          <Row label="撤单" value={c.cancelled} />
          <Row label="成交率"
               value={c.fill_rate == null ? DASH : fmtPct(c.fill_rate * 100)} />
        </tbody>
      </table>
      <div style={{ padding: '0 14px 14px' }}>
        <p className="soft">
          `no_verdict` 不是「还没发生」：intent 事件在业务规则跑之前就写，
          所以它的存在证明这次决策被问过，而行的状态证明它没被回答。
        </p>
        {v.open?.length ? (
          <table className="table">
            <thead>
              <tr><th>#</th><th>标的</th><th>状态</th><th>开于</th></tr>
            </thead>
            <tbody>
              {v.open.map((e) => (
                <tr key={e.id}>
                  <td>{e.id}</td>
                  <td>{e.code || '（无标的）'}</td>
                  <td>{e.status}</td>
                  <td>{e.opened_at || DASH}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </div>
    </WorkspaceCard>
  )
}

/* ── 三类结果：互不覆盖 ──────────────────────────────────────────── */
function Outcomes({ sec }) {
  const v = sec?.value || {}
  const counts = v.counts || {}
  const kinds = Object.keys(counts)
  const states = [...new Set(kinds.flatMap((k) => Object.keys(counts[k] || {})))]
  return (
    <WorkspaceCard title="三类结果" sec={sec}
                   subtitle="预测 / 交易 / 过程各有自己的生命周期，这里不合并">
      <table className="table">
        <thead>
          <tr>
            <th>类型</th>
            {states.map((s) => <th className="num" key={s}>{STATE_LABEL[s] || s}</th>)}
          </tr>
        </thead>
        <tbody>
          {kinds.map((k) => (
            <tr key={k}>
              <td><b>{KIND_LABEL[k] || k}</b></td>
              {states.map((s) => (
                <td className="num" key={s}>{counts[k]?.[s] ?? 0}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <div style={{ padding: '0 14px 14px' }}>
        <p className="soft">
          待定标签 {v.pending?.length ?? 0} 条（还没有后继标签的那一批）。
        </p>
        <Integrity items={v.integrity} />
      </div>
    </WorkspaceCard>
  )
}

/* ── 候选知识：隔离区 ────────────────────────────────────────────── */
function Candidates({ sec }) {
  const v = sec?.value || {}
  const counts = v.counts || {}
  return (
    <WorkspaceCard title="候选知识（隔离区）" sec={sec}
                   subtitle="提案、五态生命周期、以及推动它的迁移记录">
      <table className="table">
        <tbody>
          {Object.entries(counts).map(([status, n]) => (
            <Row key={status} label={CANDIDATE_LABEL[status] || status} value={n} />
          ))}
        </tbody>
      </table>
      <div style={{ padding: '0 14px 14px' }}>
        <p className="soft">
          这条链**刻意**不由管线驱动：`advance_candidate` 是 status 的唯一写者，
          只有人或脚本能推进它 —— 「验证不等于授权」的代价就是这里长期为空。
        </p>
        <Integrity items={v.integrity} />
      </div>
    </WorkspaceCard>
  )
}

/* ── 评估货币：当前最要紧的一个数字 ─────────────────────────────── */
function Forecasts({ sec }) {
  const v = sec?.value || {}
  const gap = (v.rows ?? 0) - (v.brier_scored ?? 0)
  const stats = v.recent_hit_rate || {}
  return (
    <WorkspaceCard title="预测与评估货币" sec={sec}
                   subtitle="校准曲线是用 brier 画的，不是用命中率画的">
      <table className="table">
        <tbody>
          <Row label="预测行数" value={v.rows} />
          <Row label="已评命中（hit）" value={v.scored} />
          <Row label="已有 Brier" value={v.brier_scored} />
          <Row label="近 30 日命中率"
               value={stats.total ? `${stats.hit_rate}%` : DASH} />
        </tbody>
      </table>
      <div style={{ padding: '0 14px 14px' }}>
        <p className={gap > 0 ? 'thesis-warn' : 'soft'}>
          {gap > 0
            ? `还没有评估货币：${gap} 行没有 Brier。没有 brier 就没有配对检验的货币，`
              + '闸门只能 abstain —— 这不是「暂时没数据」，是当前最要紧的缺口。'
            : '每一条预测都有 Brier 了。'}
        </p>
        {stats.by_confidence
          ? (
            <table className="table">
              <thead><tr><th>自报置信度</th><th className="num">条数</th><th className="num">命中率</th></tr></thead>
              <tbody>
                {Object.entries(stats.by_confidence).map(([c, s]) => (
                  <tr key={c}>
                    <td>{c}</td>
                    <td className="num">{s.total}</td>
                    <td className="num">{s.hit_rate}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
      </div>
    </WorkspaceCard>
  )
}

export default function LearnView({ learn }) {
  const sections = learn?.sections || {}
  const f = sections.forecasts?.value || {}
  const cover = sections.episodes?.value?.coverage || {}
  return (
    <section className="view active">
      <WorkspaceHead
        title="学习日志"
        payload={learn}
        blurb="一次决策的完整一生：问了什么、成了什么、留下了什么教训。三类结果分开记，
               因为一次决策可以同时是「预测对了」和「交易亏了」。" />

      <div className="kpi-strip">
        <Kpi label="决策数" value={cover.decisions ?? DASH}
             note={cover.fill_rate == null
               ? '尚无决策'
               : `成交率 ${fmtPct(cover.fill_rate * 100)}`} />
        <Kpi label="已成交" value={cover.traded ?? DASH}
             note={`撤单 ${cover.cancelled ?? DASH}`} />
        <Kpi label="预测行数" value={f.rows ?? DASH}
             note={`已评命中 ${f.scored ?? DASH}`} />
        <Kpi label="已有 Brier" value={f.brier_scored ?? DASH}
             valueClass={f.rows && !f.brier_scored ? 'down' : ''}
             note={f.rows && !f.brier_scored
               ? '评估货币为空'
               : '可与基准配对检验'} />
      </div>

      <Episodes sec={sections.episodes} />
      <Outcomes sec={sections.outcomes} />
      <Candidates sec={sections.candidates} />
      <Forecasts sec={sections.forecasts} />
    </section>
  )
}
