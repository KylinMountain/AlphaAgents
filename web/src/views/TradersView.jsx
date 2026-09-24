import { useCallback, useEffect, useMemo, useState } from 'react'

/* Traders and their handbooks.
 *
 * A trader is a YAML file under traders/; the form writes that file. The
 * built-in default has no file — it runs when no other trader is enabled, and
 * has no persona by design.
 *
 * The handbook (data/traders/<id>/MEMORY.md) is written by the trader itself
 * after each close review. A person may edit it: every decision loads it, and
 * the next rewrite starts from the edited text. The evidence line under each
 * rule is computed from market data — editing it does not change the market. */

async function api(url, opts = {}) {
  const res = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...opts })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(body.detail || `${url} → ${res.status}`)
  return body
}

/** Line diff by longest common subsequence. Handbooks are tens of lines. */
function diffLines(a, b) {
  const x = a.split('\n')
  const y = b.split('\n')
  const n = x.length
  const m = y.length
  const L = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      L[i][j] = x[i] === y[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1])
    }
  }
  const out = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (x[i] === y[j]) { out.push([' ', x[i]]); i++; j++ }
    else if (L[i + 1][j] >= L[i][j + 1]) { out.push(['-', x[i]]); i++ }
    else { out.push(['+', y[j]]); j++ }
  }
  while (i < n) out.push(['-', x[i++]])
  while (j < m) out.push(['+', y[j++]])
  return out
}

const EMPTY = {
  name: '', capital: 1000000, prompt_file: 'morning_scan.md', default_size_pct: 0.03,
  max_position_pct: 0.1, default_horizon_days: 5, enabled: true, note: '', tags: '',
  extra_prompt: '',
}

function TraderForm({ trader, promptFiles, onSaved }) {
  const [f, setF] = useState(() => ({
    ...EMPTY, ...trader.fields,
    tags: (trader.fields?.tags || []).join?.(', ') ?? trader.fields?.tags ?? '',
  }))
  const [msg, setMsg] = useState('')
  const set = (k) => (e) => setF((s) => ({
    ...s, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value,
  }))

  const save = async () => {
    setMsg('')
    try {
      await api(`/api/settings/traders/${trader.id}`, {
        method: 'PUT', body: JSON.stringify({ fields: f }),
      })
      setMsg('已保存。调度器重启后生效。')
      onSaved()
    } catch (e) {
      setMsg(e.message)
    }
  }

  const num = (k, label, help, step) => (
    <div className="set-field">
      <label className="set-label">{label}<code>{k}</code></label>
      <input className="set-input" type="number" step={step} value={f[k]} onChange={set(k)} />
      {help && <p className="set-help">{help}</p>}
    </div>
  )

  return (
    <article className="card pad">
      <div className="card-title">
        <h3>配置 · <code>traders/{trader.file || `${trader.id}.yaml`}</code></h3>
        <label className="set-switch">
          <input type="checkbox" checked={Boolean(f.enabled)} onChange={set('enabled')} />
          <span>{f.enabled ? '启用' : '停用'}</span>
        </label>
      </div>
      <div className="set-two">
        <div className="set-field">
          <label className="set-label">名字<code>name</code></label>
          <input className="set-input" value={f.name} onChange={set('name')} />
        </div>
        <div className="set-field">
          <label className="set-label">提示词模板<code>prompt_file</code></label>
          <select className="set-input" value={f.prompt_file} onChange={set('prompt_file')}>
            {promptFiles.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </div>
        {num('capital', '本金（元）', '和其他交易员分开记账', 10000)}
        {num('default_horizon_days', '默认持有期（天）', '论点没写期限时使用', 1)}
        {num('default_size_pct', '默认仓位（比例）', '0.03 = 3%', 0.01)}
        {num('max_position_pct', '单票上限（比例）', '兜底，不是目标', 0.01)}
      </div>
      <div className="set-field">
        <label className="set-label">策略（你是谁、看什么、偏好什么、什么时候不出手）<code>extra_prompt</code></label>
        <textarea className="set-input set-area" rows={10} value={f.extra_prompt}
                  onChange={set('extra_prompt')}
                  placeholder="晨扫和卖出决策都会读到这段话。介入价、止损、仓位由 agent 自己算，这里写的是判断的方式。" />
      </div>
      <div className="set-two">
        <div className="set-field">
          <label className="set-label">标签<code>tags</code></label>
          <input className="set-input" value={f.tags} onChange={set('tags')} placeholder="逗号分隔" />
        </div>
        <div className="set-field">
          <label className="set-label">备注<code>note</code></label>
          <input className="set-input" value={f.note} onChange={set('note')} />
        </div>
      </div>
      {!f.enabled && trader.running && (
        <p className="set-help">停用后如果它还有持仓，系统会继续管理这些旧仓（「仅清理旧仓」），不再开新仓。</p>
      )}
      <div className="set-actions">
        <button className="btn primary" type="button" onClick={save}>保存交易员</button>
        {msg && <span className="set-help">{msg}</span>}
      </div>
    </article>
  )
}

function Handbook({ id }) {
  const [view, setView] = useState(null)
  const [text, setText] = useState('')
  const [version, setVersion] = useState('')
  const [other, setOther] = useState('')
  const [msg, setMsg] = useState('')

  const load = useCallback(() => {
    api(`/api/settings/traders/${id}/handbook`).then((v) => {
      setView(v)
      setText(v.text)
      setVersion(v.versions[0] || '')
    }).catch((e) => setMsg(e.message))
  }, [id])
  useEffect(load, [load])

  useEffect(() => {
    if (!version) return
    api(`/api/settings/traders/${id}/handbook/${version}`)
      .then((v) => setOther(v.text)).catch(() => setOther(''))
  }, [id, version])

  const diff = useMemo(() => (version ? diffLines(other, text) : []), [other, text, version])
  const changed = view && text !== view.text

  const save = async () => {
    setMsg('')
    try {
      const v = await api(`/api/settings/traders/${id}/handbook`, {
        method: 'PUT', body: JSON.stringify({ text }),
      })
      setView(v)
      setMsg('已保存。下一次决策就会读到。')
    } catch (e) {
      setMsg(e.message)
    }
  }

  if (!view) return <article className="card pad"><p className="empty-note">{msg || '加载中…'}</p></article>

  return (
    <article className="card pad" style={{ marginTop: 14 }}>
      <div className="card-title">
        <h3>交易守则 · <code>MEMORY.md</code></h3>
        <span>{view.versions.length ? `${view.versions.length} 个历史版本` : '还没有版本'}</span>
      </div>
      <p className="set-help">
        每天 19:00 收盘复盘后由它自己改写，每次决策都会加载。你可以直接修改，它下一次改写时会以你的版本为起点。
        每条下面的「证据」由系统按日线计算，改这一行不会改变市场。
      </p>
      {!view.text && !view.versions.length ? (
        <p className="empty-note">它还没写过守则——第一次收盘复盘之后这里才会有内容。</p>
      ) : (
        <div className="set-two">
          <div>
            <textarea className="set-input set-area set-mono" rows={24} value={text}
                      onChange={(e) => setText(e.target.value)} />
            <div className="set-actions">
              <button className="btn primary" type="button" disabled={!changed} onClick={save}>保存守则</button>
              <button className="btn" type="button" disabled={!changed} onClick={() => setText(view.text)}>还原</button>
              {msg && <span className="set-help">{msg}</span>}
            </div>
          </div>
          <div>
            <div className="set-actions" style={{ marginTop: 0 }}>
              <span className="set-help">对比版本</span>
              <select className="set-input set-narrow" value={version} onChange={(e) => setVersion(e.target.value)}>
                {view.versions.map((v) => <option key={v} value={v}>{v}</option>)}
              </select>
              <span className="set-help">→ 左边</span>
            </div>
            <pre className="set-diff">
              {diff.map(([op, line], k) => (
                <div key={k} className={op === '+' ? 'add' : op === '-' ? 'del' : ''}>
                  {op} {line}
                </div>
              ))}
            </pre>
          </div>
        </div>
      )}
    </article>
  )
}

export default function TradersView() {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [sel, setSel] = useState('default')
  const [newId, setNewId] = useState('')

  const load = useCallback(() => {
    api('/api/settings/traders').then(setData).catch((e) => setError(e.message))
  }, [])
  useEffect(load, [load])

  if (!data) {
    return <section className="view active"><div className="card pad"><p className="empty-note">{error || '加载中…'}</p></div></section>
  }

  const list = [...data.traders]
  if (sel && !list.find((t) => t.id === sel)) {
    list.push({ id: sel, builtin: false, fields: {}, fresh: true })
  }
  const trader = list.find((t) => t.id === sel) || list[0]

  const create = () => {
    const id = newId.trim()
    if (!/^[a-z][a-z0-9_]{0,31}$/.test(id) || id === 'default') {
      setError('id 只能是小写字母开头的字母、数字、下划线（最多 32 位），且不能是 default')
      return
    }
    setError('')
    setSel(id)
    setNewId('')
  }

  return (
    <section className="view active">
      <div className="page-head">
        <div>
          <h1>交易员</h1>
          <p>一个交易员就是 <code>traders/</code> 下的一个 YAML 文件。没有启用的文件时，只跑没有人设的默认交易员。</p>
        </div>
      </div>
      {error && <div className="set-banner bad">{error}</div>}
      <div className="set-traders">
        <aside className="card pad">
          {list.map((t) => (
            <button key={t.id} type="button"
                    className={`set-trader ${t.id === trader.id ? 'active' : ''}`}
                    onClick={() => setSel(t.id)}>
              <b>{t.name || t.id}</b>
              <span>
                {t.builtin ? '内置' : t.fresh ? '新建（未保存）' : t.file}
                {t.legacy ? ' · 仅清理旧仓' : t.running ? ' · 运行中' : t.builtin ? '' : ' · 未运行'}
                {t.valid === false ? ' · 配置有误' : ''}
              </span>
            </button>
          ))}
          <div className="set-new">
            <input className="set-input" value={newId} placeholder="新交易员 id"
                   onChange={(e) => setNewId(e.target.value)} />
            <button className="btn" type="button" onClick={create}>新建</button>
          </div>
        </aside>
        <div>
          {trader.builtin ? (
            <article className="card pad">
              <div className="card-title"><h3>默认交易员</h3><span>{trader.running ? '运行中' : '未运行'}</span></div>
              <p className="set-help">
                没有配置文件，也没有人设或额外规矩：它每天读市场、交易、复盘，自己积累经验。
                本金、默认仓位、单票上限在「系统设置 → 交易」里改。
                有其他交易员启用时它不再开新仓，但会继续管理它自己的旧仓。
              </p>
            </article>
          ) : (
            <TraderForm key={trader.id} trader={trader} promptFiles={data.prompt_files} onSaved={load} />
          )}
          {!trader.fresh && <Handbook key={`hb-${trader.id}`} id={trader.id} />}
        </div>
      </div>
    </section>
  )
}
