import { useState, useEffect, useRef, useCallback } from 'react'

export function useWebSocket() {
  const [stages, setStages] = useState({})
  const [reports, setReports] = useState([])
  const [events, setEvents] = useState([])       // pipeline events history
  const [connected, setConnected] = useState(false)
  const wsRef = useRef(null)
  const reconnectTimer = useRef(null)

  // onclose reconnects by calling connect again. Referencing the const
  // from inside its own initialiser is a TDZ read, so the live function is
  // reached through a ref that is filled in once it exists.
  const connectRef = useRef(null)

  const connect = useCallback(() => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws`)
    wsRef.current = ws

    ws.onopen = () => setConnected(true)
    ws.onclose = () => {
      setConnected(false)
      reconnectTimer.current = setTimeout(() => connectRef.current?.(), 3000)
    }
    ws.onerror = () => ws.close()

    ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data)
        if (msg.type === 'snapshot') {
          const s = msg.data.stages || {}
          setStages(s)
          setReports(msg.data.reports || [])
        } else if (msg.type === 'event') {
          const e = msg.data
          setStages(prev => ({ ...prev, [e.stage]: e }))

          // Append to events timeline (keep last 200)
          setEvents(prev => {
            const next = [...prev, { ...e, _id: Date.now() + Math.random() }]
            return next.length > 200 ? next.slice(-200) : next
          })

          // If agent completes, refetch reports
          if (e.stage === 'agent' && e.status === 'success') {
            fetch('/api/reports')
              .then(r => r.json())
              .then(d => setReports(d.reports || []))
              .catch(() => {})
          }
        }
      } catch (err) {
        console.warn('WS parse error', err)
      }
    }
  }, [])

  useEffect(() => {
    connectRef.current = connect
    connect()
    return () => {
      clearTimeout(reconnectTimer.current)
      if (wsRef.current) wsRef.current.close()
    }
  }, [connect])

  return { stages, reports, events, connected }
}
