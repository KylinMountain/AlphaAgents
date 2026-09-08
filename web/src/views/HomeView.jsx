import { useMemo } from 'react'
import {
  DASH, bodyOf, fmtAge, fmtClock, fmtPct, fmtYi, reportHeadline,
  reportStamp, reportSummary, sourceClass, stageOf, trendClass,
  confidenceLabel,
} from '../lib/format'

/* The prototype's home screen, on real data.
 *
 * Where the prototype showed an invented number, this shows a dash and says
 * which task fills it. The point of the page is "what is the market doing",
 * and a fabricated 86% confidence answers that question wrongly rather than
 * not at all. */

function Kpi({ label, trend, trendKind = 'good', value, valueClass = '', note }) {
  return (
    <div className="kpi">
      <div className="kpi-head">
        <span>{label}</span>
        {trend && <span className={`trend ${trendKind}`}>{trend}</span>}
      </div>
      <b className={valueClass}>{value}</b>
      <small>{note}</small>
    </div>
  )
}

function riskAppetite(breadth) {
  if (!breadth) return { label: DASH, note: '盘中监控尚未抓取市场宽度' }
  const adv = breadth.advances
  const dec = breadth.declines
  if (adv == null || dec == null) {
    return { label: DASH, note: '快照缺少涨跌家数' }
  }
  const label = adv > dec * 1.5 ? 'Risk-on' : dec > adv * 1.5 ? 'Risk-off' : '分歧'
  return { label, note: `上涨 ${adv} / 下跌 ${dec}` }
}

export default function HomeView({ themes, stats, news, signals, market, reports }) {
  const breadth = market?.breadth || null
  const risk = riskAppetite(breadth)

  const topTheme = themes[0] || null
  // Sum of the sectors the API returns, which is the top slice by inflow —
  // NOT the whole market. Labelled accordingly: calling it "全市场净流入"
  // would be a number that looks authoritative and is simply wrong.
  const sectorCount = (market?.sectors || []).length
  const netFlow = useMemo(() => {
    const rows = market?.sectors || []
    if (!rows.length) return null
    return rows.reduce((a, r) => a + (Number(r.net_flow_yi) || 0), 0)
  }, [market])

  // The report's own events, when it has them. Task reports (morning,
  // review) carry none; monitor cycles do.
  const events = useMemo(() => {
    if (!latestReport?.events_json) return []
    try {
      const raw = JSON.parse(latestReport.events_json)
      if (!Array.isArray(raw)) return []
      return raw
        .map((e) => {
          const a = (e.market_impact || {}).a_share || {}
          return {
            event: e.event || '',
            category: e.category || '',
            importance: Number(e.importance) || 0,
            bullish: (a.sectors_bullish || []).slice(0, 4),
            bearish: (a.sectors_bearish || []).slice(0, 3),
          }
        })
        .filter((e) => e.event)
        .sort((a, b) => b.importance - a.importance)
    } catch {
      return []
    }
  }, [latestReport])

  const latestReportUnused = useMemo(() => {
    const sorted = [...reports].sort(
      (a, b) => (b.timestamp || 0) - (a.timestamp || 0) || (b.id || 0) - (a.id || 0),
    )
    return sorted[0] || null
  }, [reports])

  // News only. Interleaving the intraday picks here put an input (a flash
  // someone published) and a conclusion (a stock this system chose) in one
  // chronological list, where nothing distinguishes the fact from the
  // judgement. The picks have their own card below, and their own page.
  const feed = useMemo(
    () => news.slice(0, 14).map((n) => ({
      time: n.time, source: n.source, title: n.title, body: bodyOf(n),
    })),
    [news],
  )

  return (
    <section className="view active">
      <div className="page-head">
        <div>
          <h1>今天市场在交易什么？</h1>
          <p>先看主线与资金，再展开新闻和标的。每个数字都来自实际抓取，缺数据就显示 {DASH}。</p>
        </div>
        <div className="date-note">
          <b>{new Date().toLocaleDateString('zh-CN', {
            year: 'numeric', month: 'long', day: 'numeric', weekday: 'long',
          })}</b>
          {market?.captured_at ? `行情快照 ${fmtAge(market.captured_at)}` : '尚无行情快照'}
        </div>
      </div>

      <div className="kpi-strip">
        <Kpi label="市场风险偏好" value={risk.label} note={risk.note} />
        <Kpi
          label="最强主线"
          value={topTheme ? topTheme.name : DASH}
          note={topTheme ? `强度 ${topTheme.strength}` : '主线由复盘/夜扫写入'}
        />
        <Kpi
          label={sectorCount ? `前 ${sectorCount} 行业净流入` : '行业资金净流入'}
          value={fmtYi(netFlow)}
          valueClass={trendClass(netFlow)}
          note={market?.captured_at ? `快照 ${fmtClock(market.captured_at)}` : '盘中监控未运行'}
        />
        <Kpi
          label="近 7 日预测命中率"
          trend={stats?.total ? `${stats.hits} / ${stats.total}` : null}
          trendKind="warn"
          value={stats?.total ? `${stats.hit_rate}%` : DASH}
          note={stats?.total ? '已评分预测' : '尚无已评分预测'}
        />
      </div>

      <div className="grid g12">
        {/* alignSelf: the grid stretches a row's items to its tallest
            member, so this card grew to the news rail's height and ended
            in a screenful of empty box. */}
        <article className="card alpha-brief" style={{ alignSelf: 'start' }}>
          <div className="section-kicker">
            最新报告 · {latestReport ? fmtClock(reportStamp(latestReport)) : DASH}
          </div>
          {latestReport ? (
            <>
              <div className="brief-top">
                <div className="brief-main">
                  <h2>{reportHeadline(latestReport.report_text)}</h2>
                </div>
                <div className="confidence">
                  <span>覆盖事件</span>
                  <b>{latestReport.event_count ?? DASH}</b>
                  <em>{fmtAge(reportStamp(latestReport))}</em>
                </div>
              </div>

              {/* The digest already produced structured events — category,
                  importance, and the sectors it read as helped or hurt.
                  Rendering the report as one paragraph threw all of that
                  away and left a wall of text with no scannable structure. */}
              {events.length > 0 ? (
                <div className="event-list">
                  {events.slice(0, 4).map((e, i) => (
                    <div className="event-row" key={i}>
                      <span className={`severity ${
                        e.importance >= 5 ? 'high' : e.importance >= 4 ? 'mid' : 'low'}`}>
                        {e.importance}/5
                      </span>
                      <div className="event-body">
                        <div className="event-title">
                          <span className="stage-chip stage-active">
                            {e.category || '未分类'}
                          </span>
                          {e.event}
                        </div>
                        {(e.bullish.length > 0 || e.bearish.length > 0) && (
                          <div className="event-sectors">
                            {e.bullish.map((x) => (
                              <span className="sector-chip rise" key={`+${x}`}>{x}</span>
                            ))}
                            {e.bearish.map((x) => (
                              <span className="sector-chip fall" key={`-${x}`}>{x}</span>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="brief-summary">{reportSummary(latestReport.report_text)}</p>
              )}

              <div className="evidence">
                <div><span>主线数量</span><b>{themes.length || DASH}</b></div>
                <div><span>今日盘中信号</span><b>{signals.length || DASH}</b></div>
                <div><span>快讯缓存</span><b>{news.length || DASH}</b></div>
                <div><span>已评分预测</span><b>{stats?.total || DASH}</b></div>
              </div>
            </>
          ) : (
            <p className="empty-note">
              尚无报告。晨扫 09:00、盘中追因、复盘 15:30、夜扫 20:00 会写入这里。
            </p>
          )}
        </article>

        {/* The rail sets the row height, so with an empty brief beside it
            a 580px feed left half the screen blank. */}
        <aside className="card intel-rail"
               style={latestReport ? undefined : { maxHeight: 320 }}>
          <div className="card-title" style={{ padding: '13px 14px 0', margin: 0 }}>
            <h3>实时快讯</h3>
            <span>{news.length} 条缓存</span>
          </div>
          <div className="feed">
            {feed.length === 0 && <p className="empty-note" style={{ padding: '12px' }}>暂无内容</p>}
            {feed.map((item, i) => (
              <div className="feed-item" key={`${item.time}-${i}`}>
                <div className="time">{fmtClock(item.time)}</div>
                <div>
                  <div className="headline">
                    <span className={`source-tag ${sourceClass(item.source)}`}>
                      {item.source}
                    </span>
                    {item.title}
                  </div>
                  {item.body && <div className="summary">{item.body}</div>}
                </div>
              </div>
            ))}
          </div>
        </aside>

        <article className="card pad theme-card">
          <div className="card-title">
            <h3>主线雷达</h3><span>生命周期 + 强度</span>
          </div>
          {themes.length === 0 ? (
            <p className="empty-note">尚无主线。复盘与夜扫会创建和更新主线。</p>
          ) : (
            <div className="theme-list">
              {themes.slice(0, 6).map((t) => {
                const stage = stageOf(t.status)
                return (
                  <div className="theme-row" key={t.id ?? t.name}>
                    <div className="theme-name">
                      <b>{t.name}</b><span>强度 {t.strength}</span>
                    </div>
                    <span className={`stage-chip ${stage.cls}`}>{stage.label}</span>
                    <div className="bar">
                      <i style={{ width: `${Math.min(100, Math.max(0, t.strength * 10))}%` }} />
                    </div>
                    <small title={t.catalyst || ''} style={{
                      overflow: 'hidden', textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}>
                      {t.catalyst || DASH}
                    </small>
                    <small>{t.leader_code || DASH}</small>
                  </div>
                )
              })}
            </div>
          )}
        </article>

        <article className="card pad anomaly-card">
          <div className="card-title">
            <h3>今日盘中信号</h3><span>{signals.length} 条</span>
          </div>
          {signals.length === 0 ? (
            <p className="empty-note">
              盘中追因在 09:30–15:00 每 5 分钟检测一次，检测到异动才产出信号。
            </p>
          ) : (
            <div className="anomaly-list">
              {signals.slice(0, 5).map((s) => (
                <div className="anomaly" key={s.id ?? `${s.code}-${s.created_at}`}>
                  <div className="anomaly-top">
                    <b>{s.name || s.code}</b>
                    <span className={`severity ${s.confidence === 'signal' ? 'high' : 'mid'}`}>
                      {confidenceLabel(s.confidence)}
                    </span>
                  </div>
                  <div className="meta">
                    {fmtClock(s.created_at)} · {s.theme_line || s.theme || '未归类'}
                    {s.entry_price != null && ` · 记录价 ${s.entry_price}`}
                  </div>
                  {s.reason && <div className="anomaly-conclusion">{s.reason}</div>}
                </div>
              ))}
            </div>
          )}
        </article>

        <article className="card pad" style={{ gridColumn: 'span 12' }}>
          <div className="card-title">
            <h3>板块资金</h3>
            <span>{market?.captured_at ? `快照 ${fmtClock(market.captured_at)}` : '无快照'}</span>
          </div>
          {(market?.sectors || []).length === 0 ? (
            <p className="empty-note">盘中监控未运行，没有板块资金快照。</p>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr><th>板块</th><th>涨跌</th><th>净流入</th><th>龙头</th><th>龙头涨跌</th></tr>
                </thead>
                <tbody>
                  {market.sectors.map((s) => (
                    <tr key={s.sector_name}>
                      <td><b>{s.sector_name}</b></td>
                      <td className={trendClass(s.change_pct)}>{fmtPct(s.change_pct, 2)}</td>
                      <td className={trendClass(s.net_flow_yi)}>{fmtYi(s.net_flow_yi)}</td>
                      <td>{s.leader || DASH}</td>
                      <td className={trendClass(s.leader_change_pct)}>
                        {fmtPct(s.leader_change_pct, 2)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </article>
      </div>
    </section>
  )
}
