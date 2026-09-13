import { DASH } from '../lib/format'

/* One read-model section, with its three states rendered as three things.
 *
 * The API answers `unavailable` / `empty` / `present` for a reason: on the
 * same day, `policy_versions` does not exist at all, `episodes` exists and
 * holds nothing, and `virtual_portfolio` holds 52 rows. A page that shows
 * one grey "暂无数据" for all three turns "never wired" into "no news" —
 * which is the failure this repository keeps finding at the data layer, one
 * level up.
 *
 * So `unavailable` prints *which* table or column is missing, and says
 * whether the table was never created or the schema is older than the code.
 * That is the difference between "run the pipeline" and "restart on current
 * code", and a reader cannot tell them apart from an empty list.
 */

const SCHEMA_CHIP = {
  absent: { text: '表未建立', cls: 'stage-fade' },
  partial: { text: 'schema 落后于代码', cls: 'stage-sprout' },
  complete: { text: 'schema 就绪', cls: 'stage-active' },
}

const STATE_CHIP = {
  unavailable: { text: '读不到', cls: 'stage-fade' },
  empty: { text: '表在 · 0 行', cls: 'stage-sprout' },
  present: { text: '有数据', cls: 'stage-main' },
}

export function Chip({ kind, value }) {
  const table = kind === 'schema' ? SCHEMA_CHIP : STATE_CHIP
  const chip = table[value]
  if (!chip) return null
  return <span className={`stage-chip ${chip.cls}`}>{chip.text}</span>
}

export function SectionMeta({ sec }) {
  return (
    <span>
      <Chip kind="schema" value={sec.schema} />
      {' '}
      <Chip kind="state" value={sec.state} />
    </span>
  )
}

/* The blocked body: what was asked for, what is missing, who owns it.
 *
 * Exported because a section is not always exactly one card. `trade.book`
 * is the clearest case: it is five cards (positions, pending orders, closed,
 * by-theme, per-trader) and none of them can tell "the table is gone" from
 * "the table is empty". */
export function Blocked({ sec }) {
  const absent = sec.schema === 'absent'
  return (
    <div className="ws-blocked">
      <div className="ws-blocked-head">
        <b>{absent
          ? '这些表在这个库里不存在'
          : '表在，但这个库的 schema 落后于代码'}</b>
        <Chip kind="schema" value={sec.schema} />
      </div>
      <p>{sec.status_note}</p>
      <ul className="ws-missing">
        {(sec.missing || []).map((m) => <li key={m}><code>{m}</code></li>)}
      </ul>
      {sec.note ? <p className="soft">{sec.note}</p> : null}
      <p className="soft">来源：<code>{sec.source}</code></p>
    </div>
  )
}

export function WorkspaceCard({ title, subtitle, sec, children }) {
  if (!sec) return null
  return (
    <article className="card table-wrap" style={{ marginBottom: 14 }}>
      <div className="card-title" style={{ padding: '14px 14px 0' }}>
        <h3>{title}</h3>
        <SectionMeta sec={sec} />
      </div>
      {sec.state === 'unavailable' ? <Blocked sec={sec} />
        : sec.state === 'empty' ? (
          <div style={{ padding: '10px 14px 14px' }}>
            <p className="empty-note" style={{ margin: 0 }}>
              {subtitle || '表在、0 行'}
              {' '}—— 这是合法状态，不是缺失。
            </p>
            {sec.note ? (
              <p className="soft" style={{ margin: '8px 0 0' }}>{sec.note}</p>
            ) : null}
            <p className="soft" style={{ margin: '8px 0 0' }}>
              来源：<code>{sec.source}</code>
            </p>
          </div>
        ) : children}
    </article>
  )
}

/* A workspace's page head: what it is, and what state it is in right now.
 *
 * The counts are derived from the payload, not typed: a page that claims
 * four sections and renders three is a page nobody can trust the rest of.
 *
 * A missing payload is its own state, not a zero one. Printing
 * "0 有数据 · 0 空 · 0 读不到" for a workspace that never arrived says
 * "nothing is wrong", when the truth is "we do not know what the sections
 * are". Those two send you to different places — the first to the tables,
 * the second to the request.
 */
export function WorkspaceHead({ title, blurb, payload }) {
  const sections = Object.values(payload?.sections || {})
  const counts = sections.reduce((a, s) => {
    a[s.state] = (a[s.state] || 0) + 1
    return a
  }, {})
  return (
    <div className="page-head">
      <div>
        <h1>{title}</h1>
        <p>{blurb}</p>
      </div>
      {payload ? (
        <div className="date-note">
          <b>
            {counts.present || 0} 有数据 · {counts.empty || 0} 空 ·{' '}
            {counts.unavailable || 0} 读不到
          </b>
          {(payload.generated_at || '').replace('T', ' ').slice(0, 19) || DASH}
        </div>
      ) : (
        <div className="date-note ws-head-missing">
          <b>整个读模型没到</b>
          连「有哪几节」都不知道 —— 这不是「四节都坏了」，两者的排查方向不同。
        </div>
      )}
    </div>
  )
}

/* A one-line note that a fact comes from the code, not the database. */
export function CodeFact({ label, children }) {
  return (
    <div className="ws-codefact">
      <span className="name">{label}</span>
      <span>{children}</span>
    </div>
  )
}

export default WorkspaceCard
