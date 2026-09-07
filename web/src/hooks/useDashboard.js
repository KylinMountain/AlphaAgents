import { useCallback, useEffect, useState } from 'react'

/** Poll the V2 REST surface. The scheduler writes to SQLite on its own
 *  cadence (06:30 / intraday / 15:30 / weekly), so polling is enough —
 *  the WebSocket only carries V1 monitor stages. */
export function useDashboard(intervalMs = 60000) {
  const [reports, setReports] = useState([])
  const [reviews, setReviews] = useState([])
  const [sources, setSources] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    try {
      const [r, v, s] = await Promise.all([
        fetch('/api/reports').then((x) => x.json()),
        fetch('/api/reviews').then((x) => x.json()),
        // /api/sources carries display name and type on top of health.
        fetch('/api/sources').then((x) => x.json()).catch(() => ({})),
      ])
      // /api/reports returns DB rows plus anything still only in memory.
      const merged = [...(r.reports || []), ...(r.live || [])]
      setReports(merged)
      setReviews(v.reviews || [])
      setSources(s.sources || [])
      setError(null)
    } catch (e) {
      setError(e.message || 'load failed')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, intervalMs)
    return () => clearInterval(id)
  }, [load, intervalMs])

  return { reports, reviews, sources, loading, error, reload: load }
}

/** Aggregate hit rate across the returned review rows. */
export function summarizeReviews(reviews) {
  const total = reviews.reduce((a, r) => a + (r.predictions_count || 0), 0)
  const hit = reviews.reduce((a, r) => a + (r.correct_count || 0), 0)
  return {
    total,
    hit,
    accuracy: total ? hit / total : null,
    days: reviews.length,
  }
}
