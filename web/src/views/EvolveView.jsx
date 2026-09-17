import { DASH } from '../lib/format'
import { CodeFact, WorkspaceCard, WorkspaceHead } from '../components/WorkspaceSection'

/* The evolve laboratory: what is in force, what is under test, what was decided.
 *
 * Three of the four cards below are `unavailable` on the current database,
 * and that is not a rendering problem — the tables were never created,
 * because nothing has ever run. The banner at the top keeps the two reasons
 * apart, because they are fixed by different things: the *code* can now
 * promote (one candidate producer is registered, since 2026-09-13), while the
 * *data* cannot yet justify a promotion (no version is in force and no
 * experiment has run). A page that showed "0 verdicts" without saying either
 * would read as a quiet week.
 */

function Kpi({ label, value, valueClass = '', note }) {
  return (
    <div className="kpi">
      <div className="kpi-head"><span>{label}</span></div>
      <b className={valueClass}>{value}</b>
      <small>{note}</small>
    </div>
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

function ts(v) {
  return v ? String(v).replace('T', ' ').slice(0, 19) : DASH
}

/* The fact the database cannot express: can this build promote anything? */
function Reachability({ code }) {
  if (!code) return null
  const open = code.reachable
  return (
    <article className={`card pad ws-reach ${open ? 'ws-reach-open' : ''}`}
             style={{ marginBottom: 14 }}>
      <div className="card-title">
        <h3>晋升路径是否可达</h3>
        <span className={`ws-reach-verdict${open ? '' : ' is-closed'}`}>
          {open ? '可达' : '本构建不可达'}
        </span>
      </div>
      <p className={open ? 'soft' : 'thesis-warn'}>
        {open
          ? '至少有一个候选生产者已登记，闸门可以产出候选级证据。'
          : '这不来自数据库，来自代码：闸门只接受 candidate_policy 证据，'
            + '而当前登记的生产者全是基线 —— 所以这个构建能产出的每一份裁决'
            + '都是 baseline_only，并会在晋升时被拒绝。补它要先回答「候选策略是什么」，'
            + '那是设计决定，不是缺函数。'}
      </p>
      <div className="ws-codefacts">
        <CodeFact label="已登记生产者">
          {(code.producers || []).map((p) => (
            <code key={p} style={{ marginRight: 6 }}>{p}</code>
          ))}
        </CodeFact>
        <CodeFact label="其中候选生产者">
          {code.candidate_producers?.length
            ? code.candidate_producers.map((p) => <code key={p}>{p}</code>)
            : <span className="soft">无</span>}
        </CodeFact>
        <CodeFact label="晋升接受的证据等级">
          <code>{code.promotion_accepts}</code>
        </CodeFact>
        <CodeFact label="基线名称"><code>{code.baseline}</code></CodeFact>
      </div>
    </article>
  )
}

function Pointer({ sec }) {
  const v = sec?.value || {}
  const a = v.active
  return (
    <WorkspaceCard title="什么在生效" sec={sec}
                   subtitle="在效版本是一个指针，不是「最近一次批准的版本」——后者会成为第二个真相来源">
      <table className="table">
        <tbody>
          <tr>
            <td>当前生效版本</td>
            <td className="num">
              {a ? `#${a.version_id ?? a.id}` : DASH}
              {v.active_ref ? <span className="soft"> · {v.active_ref}</span> : null}
            </td>
          </tr>
          <tr><td>指针变更时间</td><td className="num">{ts(a?.changed_at)}</td></tr>
          <tr><td>变更者</td><td className="num">{a?.changed_by || DASH}</td></tr>
          <tr><td>理由</td><td className="num">{a?.reason || DASH}</td></tr>
          <tr><td>冻结版本总数</td>
            <td className="num">{v.counts?.versions ?? v.versions?.length ?? 0}</td></tr>
          <tr><td>批准记录</td><td className="num">{v.approvals?.length ?? 0}</td></tr>
          <tr><td>指针迁移记录</td>
            <td className="num">{v.transitions?.length ?? 0}</td></tr>
        </tbody>
      </table>
      <div className="card-body">
        <Integrity items={v.integrity} />
      </div>
    </WorkspaceCard>
  )
}

function Shadow({ sec }) {
  const v = sec?.value || {}
  const paired = v.paired || {}
  return (
    <WorkspaceCard title="影子实验" sec={sec}
                   subtitle="挑战者照常出预测并记录，但不驱动任何决策">
      <table className="table">
        <tbody>
          <tr><td>影子 run</td><td className="num">{v.runs?.length ?? 0}</td></tr>
          <tr>
            <td>已配对样本（可与冠军同题比较）</td>
            <td className="num">
              {Object.keys(paired).length
                ? Object.entries(paired).map(([id, n]) => (
                  <span key={id}>#{id}: {n}{' '}</span>
                ))
                : DASH}
            </td>
          </tr>
        </tbody>
      </table>
      <div className="card-body">
        {v.runs?.length ? (
          <table className="table">
            <thead>
              <tr>
                <th>#</th><th>策略版本</th><th>报告类型</th>
                <th>生产者</th><th>状态</th><th>开于</th>
              </tr>
            </thead>
            <tbody>
              {v.runs.map((r) => (
                <tr key={r.id}>
                  <td>{r.id}</td>
                  <td>#{r.policy_version_id}</td>
                  <td>{r.report_type}</td>
                  <td><code>{r.producer}</code></td>
                  <td>{r.status}</td>
                  <td>{ts(r.opened_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        <Integrity items={v.integrity} />
      </div>
    </WorkspaceCard>
  )
}

function Gates({ sec }) {
  const v = sec?.value || {}
  return (
    <WorkspaceCard title="闸门裁决" sec={sec}
                   subtitle="既要看晋升也要看弃权：被记录的拒绝才拦得住同一个提案被反复重提">
      <table className="table">
        <tbody>
          <tr><td>裁决总数</td><td className="num">{v.n ?? 0}</td></tr>
          <tr><td>晋升</td><td className="num">{v.promoted ?? 0}</td></tr>
          <tr><td>弃权（样本不足）</td><td className="num">{v.abstained ?? 0}</td></tr>
          <tr>
            <td>非弃权（真的比较过）</td>
            <td className="num">{v.non_abstain ?? 0}</td>
          </tr>
        </tbody>
      </table>
      <div className="card-body">
        {v.decisions?.length ? (
          <table className="table">
            <thead>
              <tr>
                <th>日期</th><th>候选</th><th>结果</th>
                <th className="num">n</th><th className="num">验证天数</th>
                <th>证据等级</th><th>理由</th>
              </tr>
            </thead>
            <tbody>
              {v.decisions.map((d) => (
                <tr key={d.id}>
                  <td>{d.date}</td>
                  <td><code>{d.candidate}</code></td>
                  <td>
                    <span className={`stage-chip ${
                      d.promoted ? 'stage-main' : 'stage-fade'}`}>
                      {d.promoted ? '晋升' : (d.abstained ? '弃权' : '拒绝')}
                    </span>
                  </td>
                  <td className="num">{d.n ?? DASH}</td>
                  <td className="num">{d.validation_days ?? DASH}</td>
                  <td>{d.evidence_scope
                    ? <code>{d.evidence_scope}</code> : DASH}</td>
                  <td style={{ color: 'var(--text-2)' }}>{d.reason || DASH}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </div>
    </WorkspaceCard>
  )
}

function Knowledge({ sec }) {
  const v = sec?.value || {}
  return (
    <WorkspaceCard title="已批准知识快照" sec={sec}
                   subtitle="已批准的知识快照：批准是可查的事实，生效与否由在效版本指向谁决定">
      <table className="table">
        <tbody>
          <tr><td>快照数</td><td className="num">{v.counts?.snapshots ?? 0}</td></tr>
          <tr><td>条目数</td><td className="num">{v.counts?.items ?? 0}</td></tr>
          <tr>
            <td>内容漂移</td>
            <td className="num">
              {v.drifted?.length
                ? <span className="down">{v.drifted.length} 条已漂移</span>
                : '无'}
            </td>
          </tr>
        </tbody>
      </table>
      <div className="card-body">
        {v.snapshots?.length ? (
          <table className="table">
            <thead>
              <tr>
                <th>#</th><th>批准人</th><th>批准时间</th>
                <th>冻结时间</th><th>理由</th>
              </tr>
            </thead>
            <tbody>
              {v.snapshots.map((s) => (
                <tr key={s.id}>
                  <td>{s.id}</td>
                  <td>{s.approved_by}</td>
                  <td>{ts(s.approved_at)}</td>
                  <td>{ts(s.frozen_at)}</td>
                  <td style={{ color: 'var(--text-2)' }}>{s.reason || DASH}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        <Integrity items={v.integrity} />
      </div>
    </WorkspaceCard>
  )
}

export default function EvolveView({ evolve }) {
  const sections = evolve?.sections || {}
  const gates = sections.gates?.value || {}
  const shadow = sections.shadow?.value || {}
  const pointer = sections.pointer?.value || {}
  return (
    <section className="view active">
      <WorkspaceHead
        title="进化实验台"
        payload={evolve}
        blurb="策略变更的唯一入口：冻结一版、前向影子比较、由证据决定能不能晋升。
               证据来自市场数据，不包括任何模型对自己产出的评分。" />

      <div className="kpi-strip">
        <Kpi label="生效版本"
             value={pointer.active ? `#${pointer.active.version_id ?? pointer.active.id}` : DASH}
             note={`冻结版本 ${pointer.counts?.versions ?? pointer.versions?.length ?? 0} 个`} />
        <Kpi label="影子 run" value={shadow.runs?.length ?? DASH}
             note={Object.keys(shadow.paired || {}).length
               ? '已有配对样本'
               : '尚无配对样本'} />
        <Kpi label="非弃权裁决" value={gates.non_abstain ?? DASH}
             valueClass={gates.non_abstain ? '' : 'down'}
             note={`共 ${gates.n ?? 0} 条，其中弃权 ${gates.abstained ?? 0} 条`} />
        <Kpi label="候选生产者" value={(evolve?.code?.candidate_producers || []).length}
             valueClass={(evolve?.code?.candidate_producers || []).length ? '' : 'down'}
             note={(evolve?.code?.candidate_producers || []).length
               ? '闸门可产出候选级证据'
               : '晋升在本构建里不可达'} />
      </div>

      <Reachability code={evolve?.code} />
      <Pointer sec={sections.pointer} />
      <Shadow sec={sections.shadow} />
      <Gates sec={sections.gates} />
      <Knowledge sec={sections.knowledge} />
    </section>
  )
}
