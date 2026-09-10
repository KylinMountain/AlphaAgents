import { useCallback, useEffect, useState } from 'react'

const ENDPOINTS = {
  reports: '/api/reports',
  reviews: '/api/reviews',
  sources: '/api/sources',
  activity: '/api/activity?limit=40',
  news: '/api/news?limit=150',
  themes: '/api/themes',
  stats: '/api/prediction-stats?days=7',
  signals: '/api/intraday-signals',
  market: '/api/market-overview',
  graph: '/api/event-graph',
  portfolio: '/api/portfolio',
  calibration: '/api/calibration',
  version: '/api/version',
  usage: '/api/usage?days=14',
}

async function getJson(url) {
  const res = await fetch(url)
  if (!res.ok) throw new Error(`${url} → ${res.status}`)
  return res.json()
}

/** Poll the REST surface. The scheduler writes to SQLite on its own cadence
 *  and runs in a separate container, so its in-memory event bus never
 *  reaches this page — everything here goes through the shared DB.
 *
 *  Each endpoint settles independently: one failing panel shows its own
 *  empty state rather than blanking the whole dashboard. */
export function useDashboard(intervalMs = 20000) {
  const [data, setData] = useState({
    reports: [], reviews: [], sources: [], activity: [], news: [],
    themes: [], stats: null, signals: [], market: null, graph: null,
    portfolio: null, calibration: null, version: null, usage: null,
  })
  const [failed, setFailed] = useState([])
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    const keys = Object.keys(ENDPOINTS)
    const settled = await Promise.allSettled(keys.map((k) => getJson(ENDPOINTS[k])))

    const next = {}
    const broke = []
    settled.forEach((res, i) => {
      const key = keys[i]
      if (res.status !== 'fulfilled') {
        broke.push(key)
        return
      }
      const v = res.value
      if (key === 'reports') next.reports = [...(v.reports || []), ...(v.live || [])]
      else if (key === 'reviews') next.reviews = v.reviews || []
      else if (key === 'sources') next.sources = v.sources || []
      else if (key === 'activity') next.activity = v.activity || []
      else if (key === 'news') next.news = v.news || []
      else if (key === 'themes') next.themes = v.themes || []
      else if (key === 'signals') next.signals = v.signals || []
      else next[key] = v
    })

    setData((prev) => ({ ...prev, ...next }))
    setFailed(broke)
    setLoading(false)
  }, [])

  useEffect(() => {
    // Deferred rather than called inline: load() resolves asynchronously,
    // but a call in the effect body still reads as a synchronous state
    // write to the compiler's rule, and the first paint should not block
    // on ten fetches anyway.
    const first = setTimeout(load, 0)
    const id = setInterval(load, intervalMs)
    return () => { clearTimeout(first); clearInterval(id) }
  }, [load, intervalMs])

  return { ...data, failed, loading, reload: load }
}

/** Aggregate hit rate across the returned review rows. */
export function summarizeReviews(reviews) {
  const total = reviews.reduce((a, r) => a + (r.predictions_count || 0), 0)
  const hit = reviews.reduce((a, r) => a + (r.correct_count || 0), 0)
  return { total, hit, accuracy: total ? hit / total : null, days: reviews.length }
}
