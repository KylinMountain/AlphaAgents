/* Shared formatting.
 *
 * The rule every helper here follows: absent data renders as a dash, never
 * as a zero. A dashboard whose job is to say what the market is doing must
 * not answer "0.0%" when it means "nothing captured yet" — that reads as a
 * measurement, and the reader has no way to tell the difference. */

export const DASH = '—'

/** Timestamps from the DB are Asia/Shanghai wall-clock (the containers are
 *  pinned to it), so they are parsed as local time. Appending 'Z' would
 *  shift every row eight hours. */
export function parseStamp(raw) {
  if (raw == null) return null
  const d = new Date(typeof raw === 'number' ? raw : String(raw).replace(' ', 'T'))
  return Number.isNaN(d.getTime()) ? null : d
}

export function fmtClock(raw) {
  const d = parseStamp(raw)
  if (!d) return DASH
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

export function fmtDateTime(raw) {
  const d = parseStamp(raw)
  if (!d) return DASH
  return d.toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  })
}

export function fmtAge(raw) {
  const d = parseStamp(raw)
  if (!d) return DASH
  const mins = Math.max(0, Math.round((Date.now() - d.getTime()) / 60000))
  if (mins < 1) return '刚刚'
  if (mins < 60) return `${mins} 分钟前`
  const hrs = Math.round(mins / 60)
  return hrs < 24 ? `${hrs} 小时前` : `${Math.round(hrs / 24)} 天前`
}

export function fmtPct(v, digits = 1) {
  if (v == null || Number.isNaN(Number(v))) return DASH
  const n = Number(v)
  return `${n > 0 ? '+' : ''}${n.toFixed(digits)}%`
}

export function fmtYi(v, digits = 1) {
  if (v == null || Number.isNaN(Number(v))) return DASH
  const n = Number(v)
  return `${n > 0 ? '+' : ''}${n.toFixed(digits)} 亿`
}

export function fmtNum(v) {
  if (v == null || Number.isNaN(Number(v))) return DASH
  return String(v)
}

/** Sign class for the up/down colour tokens; neutral when unknown. */
export function trendClass(v) {
  if (v == null || Number.isNaN(Number(v))) return ''
  return Number(v) > 0 ? 'up' : Number(v) < 0 ? 'down' : ''
}

export const SOURCE_TAG = {
  金十数据: 'src-jin10',
  新浪7x24: 'src-sina',
  财联社电报: 'src-cls',
  东方财富7x24: 'src-global',
  华尔街见闻: 'src-global',
}

export function sourceClass(source) {
  return SOURCE_TAG[source] || 'src-global'
}

/** Flash bodies usually repeat the headline verbatim, so showing both
 *  doubles the row height for nothing. */
export function bodyOf(item) {
  const s = (item.summary || '').trim()
  const t = (item.title || '').trim()
  if (!s || s === t) return ''
  return s.startsWith(t) ? s.slice(t.length).trim() : s
}

/** Columns that hold a JSON array arrive as a string from SQLite. Rendered
 *  raw they fill a table cell with `[{"code": "000759", …}]`, which is what
 *  the themes page did. Returns [] rather than throwing on bad JSON — a
 *  malformed row should cost its own chips, not the whole page. */
export function parseList(raw) {
  if (Array.isArray(raw)) return raw
  if (!raw || typeof raw !== 'string') return []
  try {
    const v = JSON.parse(raw)
    return Array.isArray(v) ? v : []
  } catch {
    return []
  }
}

const STAGE_LABEL = {
  main: ['主升', 'stage-main'],
  active: ['活跃', 'stage-active'],
  watching: ['观察', 'stage-active'],
  sprout: ['萌芽', 'stage-sprout'],
  fading: ['衰退', 'stage-fade'],
  fade: ['衰退', 'stage-fade'],
  archived: ['归档', 'stage-fade'],
}

export function stageOf(status) {
  const [label, cls] = STAGE_LABEL[status] || [status || '未知', 'stage-active']
  return { label, cls }
}

/** Unwrap the fences the agents put around report prose.
 *
 * The agents return their report inside a bare ``` block, so the renderer
 * showed the whole thing as literal text — headings, tables and bullets
 * verbatim. Position alone cannot identify it: the block starts after a
 * preamble sentence and ends before an appended 修订说明 section, so a
 * "fence wraps the whole body" test never matched.
 *
 * Content identifies it instead. A fence with no language tag whose body
 * carries report structure (a 【小节】 header, a markdown heading, or a
 * table row) is prose the model wrapped by habit. A tagged fence, or one
 * holding something that reads like code, is left alone.
 */
export function stripWrappingFence(text) {
  const lines = String(text).split('\n')
  const out = []
  let i = 0

  while (i < lines.length) {
    const open = /^\s*```(.*)$/.exec(lines[i])
    if (!open) {
      out.push(lines[i])
      i += 1
      continue
    }

    let close = i + 1
    while (close < lines.length && !/^\s*```\s*$/.test(lines[close])) close += 1
    if (close >= lines.length) {
      // Unterminated: everything after it would render as one code block.
      out.push(...lines.slice(i + 1))
      break
    }

    const body = lines.slice(i + 1, close)
    const tagged = open[1].trim() !== ''
    const looksLikeReport = body.some(
      (l) => /^【.+?】/.test(l.trim()) || /^#{1,6}\s/.test(l.trim())
        || /^\s*\|.+\|/.test(l),
    )
    if (!tagged && looksLikeReport) {
      out.push(...body)
    } else {
      out.push(...lines.slice(i, close + 1))
    }
    i = close + 1
  }
  return out.join('\n')
}

/** Turn an agent report into markdown the renderer can give structure to.
 *
 * The prompts produce 【小节】 headers and full-width rule lines, not
 * markdown. Rendered as-is every section became another anonymous
 * paragraph and the rules became paragraphs of dashes — a wall of text
 * with no hierarchy, which is what the report page showed.
 *
 * The conversion is deliberately conservative: only a line that is
 * entirely a 【…】 header becomes a heading, so an inline 【…】 inside a
 * sentence is left alone.
 */
export function reportToMarkdown(text) {
  if (!text) return ''
  return stripWrappingFence(String(text))
    .split('\n')
    .map((line) => {
      const t = line.trim()
      // Separator rules: three or more box-drawing/dash characters and
      // nothing else. The prompts use several of them interchangeably —
      // ═ was missing at first and left a paragraph of double lines.
      if (/^[\s─━═—–\-=_~*]{3,}$/.test(t)) return '---'
      const header = /^【(.+?)】\s*$/.exec(t)
      if (header) return `### ${header[1]}`
      // "【小节】正文" on one line: promote the header, keep the body.
      const inline = /^【(.+?)】\s*(.+)$/.exec(t)
      if (inline) return `### ${inline[1]}\n\n${inline[2]}`
      return line
    })
    .join('\n')
    // Collapse the runs of rules the separators leave behind.
    .replace(/(?:^---$\n?){2,}/gm, '---\n')
}

/** A one-line headline for a report card.
 *
 * The first non-empty line is the agent's preamble ("现在我已经收集了足够
 * 的数据，可以撰写完整的分析报告了。"), which says nothing about the
 * market. Prefer the first real section header, then the first line that
 * carries a fact.
 */
export function reportHeadline(text) {
  const lines = stripWrappingFence(String(text || '')).split('\n')
  const clean = lines
    .map((l) => l.trim())
    .filter((l) => l && !/^[\s─━═—–\-=_~*`]{3,}$/.test(l) && !/^```/.test(l))

  // The report opens with its own title bar ("AlphaAgents 分析报告 | …"),
  // which names the document rather than the market. Skip it.
  const body = clean.filter((l) => !/AlphaAgents\s*(分析|期货)?报告/.test(l))

  for (let i = 0; i < body.length; i += 1) {
    const m = /^【(.+?)】\s*(.*)$/.exec(body[i])
    if (!m) continue
    // A header alone on its line takes the line under it as its content.
    const content = m[2] || body[i + 1] || ''
    if (content) return `${m[1]}：${content}`.slice(0, 64)
  }
  const fact = body.find((l) => /[0-9%]/.test(l) && l.length > 8)
  if (fact) return fact.slice(0, 64)
  return body[0] ? body[0].slice(0, 64) : '报告已生成'
}

/** A short body for a report card: the first few lines that carry content,
 *  with the fence, rules and preamble already gone. */
export function reportSummary(text, limit = 240) {
  const headline = reportHeadline(text)
  const lines = stripWrappingFence(String(text || ''))
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l && !/^[\s─━═—–\-=_~*`]{3,}$/.test(l) && !/^```/.test(l))
    .filter((l) => !/AlphaAgents\s*(分析|期货)?报告/.test(l))

  // Everything before the first section header is the agent talking about
  // itself ("Now I have all the data needed. Let me compile the report.")
  // plus the report's own title bar. Neither describes the market.
  const firstSection = lines.findIndex((l) => /^【.+?】/.test(l))
  const afterHeader = firstSection >= 0 ? lines.slice(firstSection) : lines

  // The headline is truncated, so `includes` never matches the full line
  // it came from — compare on a prefix instead, or the card repeats
  // itself: the same sentence as title and as first line of the body.
  const headlineBody = headline.replace(/^.+?：\s*/, '')
  const isHeadlineSource = (l) => {
    const clean = l.replace(/^【.+?】\s*/, '')
    if (!clean) return false
    const head = clean.slice(0, 20)
    return head.length >= 6 && headlineBody.startsWith(head)
  }

  const body = afterHeader
    .filter((l) => !isHeadlineSource(l))
    .map((l) => l.replace(/^【(.+?)】\s*/, '$1: '))
    .join(' ')
  return body.length > limit ? `${body.slice(0, limit)}…` : body
}

const CONFIDENCE_LABEL = {
  high: '高信心', medium: '中等信心', low: '低信心',
  signal: '涨停确认',
}

const DIRECTION_LABEL = {
  bullish: '看多', bearish: '看空', neutral: '中性',
}

/** Confidence tier as Chinese. Stored in English by the pipeline; an
 *  unknown tier is passed through rather than hidden. */
export function confidenceLabel(v) {
  return CONFIDENCE_LABEL[v] || v || '未标注'
}

export function directionLabel(v) {
  return DIRECTION_LABEL[v] || v || ''
}

/** Display stamp for a report row.
 *
 * Prefer the epoch column: created_at came from SQLite's datetime('now'),
 * which is UTC, so rows written before that was fixed render eight hours
 * early. Shared so the home card and the reports page cannot disagree.
 */
export function reportStamp(r) {
  if (!r) return null
  return r.timestamp ? r.timestamp * 1000 : r.created_at
}
