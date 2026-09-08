import { useCallback, useEffect, useState } from 'react'

/** Poll the V2 REST surface. The scheduler writes to SQLite on its own
 *  cadence (06:30 / intraday / 15:30 / weekly) and runs in a separate
 *  container, so its in-memory event bus never reaches this page — the
 *  activity stream goes through the shared DB and is polled. */
export function useDashboard(intervalMs = 20000) {
  const [reports, setReports] = useState([])
  const [reviews, setReviews] = useState([])
  const [sources, setSources] = useState([])
  const [activity, setActivity] = useState([])
  const [news, setNews] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    try {
      const [r, v, s, a, n] = await Promise.all([
        fetch('/api/reports').then((x) => x.json()),
        fetch('/api/reviews').then((x) => x.json()),
        // /api/sources carries display name and type on top of health.
        fetch('/api/sources').then((x) => x.json()).catch(() => ({})),
        // Scheduler activity — written by the scheduler container, read
        // here. Feeds the status card only; the stream shows news.
        fetch('/api/activity?limit=40').then((x) => x.json()).catch(() => ({})),
        fetch('/api/news?limit=150').then((x) => x.json()).catch(() => ({})),
      ])
      // /api/reports returns DB rows plus anything still only in memory.
      const merged = [...(r.reports || []), ...(r.live || [])]
      setReports(merged)
      setReviews(v.reviews || [])
      setSources(s.sources || [])
      setActivity(a.activity || [])
      setNews(n.news || [])
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

  return { reports, reviews, sources, activity, news, loading, error, reload: load }
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
