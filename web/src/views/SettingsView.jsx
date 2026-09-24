import { useCallback, useEffect, useState } from 'react'

/* System settings: the .env file, edited in the browser.
 *
 * .env stays the one source of truth — the server rewrites it in place and
 * everything shown here is read back from it. Settings load when a process
 * starts, so every save ends with the same instruction: restart.
 *
 * The test buttons matter more than the form. This system's usual failure is
 * a pipeline that runs with an error string for content, so a model counts as
 * configured once it has answered — the reply text is shown, not a green dot. */

async function api(url, opts = {}) {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json' }, ...opts,
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(body.detail || `${url} → ${res.status}`)
  return body
}

const SOURCE = { file: '.env', environment: '进程环境', default: '默认值' }

function Field({ f, value, onChange }) {
  const id = `set-${f.key}`
  let input
  if (f.kind === 'bool') {
    const on = (value ?? f.value ?? f.default) === '1'
    input = (
      <label className="set-switch">
        <input type="checkbox" id={id} checked={on}
               onChange={(e) => onChange(e.target.checked ? '1' : '0')} />
        <span>{on ? '开启' : '关闭'}</span>
      </label>
    )
  } else {
    const secret = f.kind === 'secret'
    const cleared = value === null
    input = (
      <div className="set-input-row">
        <input id={id} className="set-input"
               type={secret ? 'password' : 'text'}
               autoComplete="off" spellCheck={false}
               value={cleared ? '' : (value ?? (secret ? '' : f.value))}
               placeholder={secret
                 ? (f.set ? `已设置 ${f.masked}（留空不改）`
                   : f.role === 'SUMMARY' ? '不填则沿用「新闻过滤」'
                   : f.role ? '未设置（沿用 SiliconFlow Key）' : '未设置')
                 : (f.default ? `默认 ${f.default}`
                   : f.role === 'SUMMARY' ? '不填则沿用「新闻过滤」' : '')}
               onChange={(e) => onChange(e.target.value)} />
        {secret && f.set && !cleared && (
          <button className="btn set-mini" type="button" onClick={() => onChange(null)}>清除</button>
        )}
        {cleared && <span className="set-flag warn">保存后删除</span>}
      </div>
    )
  }
  return (
    <div className="set-field">
      <label htmlFor={id} className="set-label">
        {f.label}
        <code>{f.key}</code>
        <span className={`set-src src-${f.source}`}>{SOURCE[f.source]}</span>
        {f.placeholder && <span className="set-flag warn">还是示例占位符</span>}
      </label>
      {input}
      {f.help && <p className="set-help">{f.help}</p>}
    </div>
  )
}

function TestResult({ r }) {
  if (!r) return null
  if (r.pending) return <p className="set-result">测试中…</p>
  return (
    <div className={`set-result ${r.ok ? 'ok' : 'bad'}`}>
      <b>{r.ok ? '可用' : '失败'}</b>
      {r.model && <span> · {r.model}</span>}
      {r.latency_ms != null && <span> · {r.latency_ms} ms</span>}
      {r.reply && <div className="set-reply">模型回复：「{r.reply}」</div>}
      {r.error && <div className="set-reply">{r.error}</div>}
    </div>
  )
}

function RoleCard({ role, fields, edits, setEdit, onTest, result }) {
  return (
    <article className="card pad set-role">
      <div className="card-title">
        <h3>{role.label}</h3>
        <span className={role.configured ? 'set-ok' : 'set-bad'}>
          {role.configured ? '已配置' : '未配置'}
        </span>
      </div>
      <p className="set-effective">
        当前生效：<b>{role.model || '—'}</b> @ {role.url || '—'}
        {role.inherited && <span>（沿用{role.inherited === 'DIGEST' ? '「新闻过滤」' : role.inherited}）</span>}
        {role.key && <span> · Key {role.key}</span>}
      </p>
      {fields.map((f) => (
        <Field key={f.key} f={f} value={edits[f.key]} onChange={(v) => setEdit(f.key, v)} />
      ))}
      <div className="set-actions">
        <button className="btn" type="button" onClick={onTest}>测试连接</button>
        <span className="set-help">用上面的值（含未保存的修改）真实调用一次</span>
      </div>
      <TestResult r={result} />
    </article>
  )
}

const CHANNELS = [
  ['feishu', '飞书'], ['dingtalk', '钉钉'], ['wecom', '企业微信'], ['telegram', 'Telegram'],
]

export default function SettingsView() {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [tab, setTab] = useState('llm')
  const [edits, setEdits] = useState({})
  const [saved, setSaved] = useState(null)
  const [tests, setTests] = useState({})

  const load = useCallback(() => {
    api('/api/settings').then(setData).catch((e) => setError(e.message))
  }, [])
  useEffect(load, [load])

  const setEdit = (k, v) => setEdits((e) => ({ ...e, [k]: v }))
  const dirty = Object.keys(edits).length

  const save = async () => {
    setError('')
    try {
      const got = await api('/api/settings', {
        method: 'PUT', body: JSON.stringify({ values: edits }),
      })
      setData(got)
      setEdits({})
      setSaved(got.saved)
    } catch (e) {
      setError(e.message)
    }
  }

  const test = async (key, url, body) => {
    setTests((t) => ({ ...t, [key]: { pending: true } }))
    try {
      const r = await api(url, { method: 'POST', body: JSON.stringify({ ...body, values: edits }) })
      setTests((t) => ({ ...t, [key]: r }))
    } catch (e) {
      setTests((t) => ({ ...t, [key]: { ok: false, error: e.message } }))
    }
  }

  if (!data) {
    return (
      <section className="view active">
        <div className="card pad"><p className="empty-note">{error || '加载中…'}</p></div>
      </section>
    )
  }

  const group = data.groups.find((g) => g.id === tab)
  const byRole = {}
  for (const f of data.groups.find((g) => g.id === 'llm').fields) {
    if (f.role) (byRole[f.role] ||= []).push(f)
  }
  const shared = data.groups.find((g) => g.id === 'llm').fields.filter((f) => !f.role)

  return (
    <section className="view active">
      <div className="page-head">
        <div>
          <h1>系统设置</h1>
          <p>写入 <code>{data.env_path}</code>。设置在进程启动时读取，保存后要重启调度器和本页面服务才生效。</p>
        </div>
        <div className="set-actions">
          {dirty > 0 && <span className="set-help">{dirty} 项未保存</span>}
          <button className="btn" type="button" disabled={!dirty} onClick={() => setEdits({})}>放弃修改</button>
          <button className="btn primary" type="button" disabled={!dirty} onClick={save}>保存</button>
        </div>
      </div>

      {!data.onboarding.agent_configured && (
        <div className="set-banner warn">
          <b>还没有配置决策模型。</b>在下面「决策 Agent」里填入 API Key、Base URL 和模型名，
          点「测试连接」看到模型回复后再保存。任何兼容 OpenAI API 的服务都可以
          （DeepSeek、DashScope、SiliconFlow、OpenRouter、本地 Ollama）。
        </div>
      )}
      {(saved != null || data.changed_since_boot) && (
        <div className="set-banner">
          <b>{saved != null ? `已保存 ${saved} 项。` : '.env 在本服务启动后被修改过。'}</b>
          重启后生效：<code>uv run python main.py run-v2</code> 和 <code>uv run python main.py web</code>
          （Docker：<code>docker compose --profile web up -d --force-recreate</code>）。
        </div>
      )}
      {error && <div className="set-banner bad">{error}</div>}

      <div className="set-tabs">
        {data.groups.map((g) => (
          <button key={g.id} type="button" className={`tab ${tab === g.id ? 'active' : ''}`}
                  onClick={() => setTab(g.id)}>{g.title}</button>
        ))}
      </div>

      {tab === 'llm' ? (
        <>
          <div className="set-grid">
            {data.roles.map((role) => (
              <RoleCard key={role.role} role={role} fields={byRole[role.role] || []}
                        edits={edits} setEdit={setEdit}
                        result={tests[role.role]}
                        onTest={() => test(role.role, '/api/settings/test-llm', { role: role.role })} />
            ))}
          </div>
          <article className="card pad" style={{ marginTop: 14 }}>
            <div className="card-title"><h3>共用</h3><span>各角色没填 Key 时使用</span></div>
            {shared.map((f) => (
              <Field key={f.key} f={f} value={edits[f.key]} onChange={(v) => setEdit(f.key, v)} />
            ))}
            <p className="set-help">
              OpenRouter 的 <code>:free</code> 模型是每个账号每天 50 次请求，换哪个免费模型都共用这 50 次；
              一次晨扫就要约 10 次，跑不满一天。遇到 429 时换模型没有用。
            </p>
          </article>
        </>
      ) : (
        <article className="card pad" style={{ marginTop: 14 }}>
          {group.fields.map((f) => (
            <div key={f.key}>
              <Field f={f} value={edits[f.key]} onChange={(v) => setEdit(f.key, v)} />
              {tab === 'notify' && f.on && f.key !== 'NOTIFY_TELEGRAM_BOT_TOKEN' && (
                <div className="set-actions">
                  <button className="btn set-mini" type="button"
                          onClick={() => test(f.on, '/api/settings/test-notify', { channel: f.on })}>
                    发送测试消息到{CHANNELS.find(([c]) => c === f.on)?.[1]}
                  </button>
                  <TestResult r={tests[f.on]} />
                </div>
              )}
            </div>
          ))}
        </article>
      )}
    </section>
  )
}
