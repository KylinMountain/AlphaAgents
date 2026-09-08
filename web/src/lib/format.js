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
  return String(text)
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
