import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { api, ApiError, setCsrfToken } from './api/client'
import type { Bootstrap } from './api/types'
import { openEventStream } from './events/stream'
import type { ConnectionState } from './events/stream'

interface AppState { data: Bootstrap | null; loading: boolean; error: Error | null; connection: ConnectionState; syncedAt: Date | null; revision: number; refresh: () => Promise<void>; signOut: () => Promise<void> }
const StateContext = createContext<AppState | null>(null)
export function AppProvider({ children }: { children: ReactNode }) {
  const [data, setData] = useState<Bootstrap | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)
  const [connection, setConnection] = useState<ConnectionState>('connecting')
  const [syncedAt, setSyncedAt] = useState<Date | null>(null)
  const [revision, setRevision] = useState(0)
  const [streamGeneration, setStreamGeneration] = useState(0)
  const flight = useRef<Promise<void> | null>(null)
  const again = useRef(false)
  const initialCursor = useRef(0)
  const needsStreamReset = useRef(false)
  const retryTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const hasSession = useRef(false)
  const sessionGeneration = useRef(0)
  const refresh = useCallback((): Promise<void> => {
    if (flight.current) { again.current = true; return flight.current }
    clearTimeout(retryTimer.current)
    const load = async () => {
      let successful = false
      do {
        again.current = false
        const generation = sessionGeneration.current
        try {
          await api.me()
          const snapshot = await api.bootstrap()
          if (generation !== sessionGeneration.current) continue
          clearTimeout(retryTimer.current)
          initialCursor.current = snapshot.cursor
          hasSession.current = true
          setData(snapshot); setError(null); setSyncedAt(new Date()); setRevision(value => value + 1)
          successful = true
        } catch (err) {
          successful = false
          if (generation !== sessionGeneration.current) continue
          setError(err instanceof Error ? err : new Error('无法加载学习工作台。'))
          if (err instanceof ApiError && err.status === 401) { setData(null); hasSession.current = false }
          // SSE may have acknowledged the final event before this snapshot failed.
          // Retry the snapshot even if no later business event arrives.
          else if (hasSession.current) retryTimer.current = setTimeout(() => { void refresh() }, 3000)
        } finally { setLoading(false) }
      } while (again.current)
      if (successful && needsStreamReset.current) { needsStreamReset.current = false; setStreamGeneration(value => value + 1) }
    }
    flight.current = load().finally(() => { flight.current = null })
    return flight.current
  }, [])
  useEffect(() => {
    const expired = () => {
      sessionGeneration.current += 1; hasSession.current = false
      clearTimeout(retryTimer.current)
      setData(null); setCsrfToken(''); setError(new ApiError(401, '登录已过期，请重新登录。'))
    }
    window.addEventListener('session-expired', expired)
    void refresh()
    return () => { window.removeEventListener('session-expired', expired); clearTimeout(retryTimer.current) }
  }, [refresh])
  const userId = data?.user.id
  useEffect(() => {
    if (!userId) return
    return openEventStream(initialCursor.current, () => { void refresh() }, setConnection, () => {
      needsStreamReset.current = true; setConnection('reconnecting'); void refresh()
    }, () => { void refresh() })
  }, [userId, streamGeneration, refresh])
  useEffect(() => {
    const reconnect = () => { if (document.visibilityState === 'visible') void refresh() }
    document.addEventListener('visibilitychange', reconnect)
    window.addEventListener('online', reconnect)
    return () => { document.removeEventListener('visibilitychange', reconnect); window.removeEventListener('online', reconnect) }
  }, [refresh])
  const signOut = async () => {
    await api.logout()
    sessionGeneration.current += 1; hasSession.current = false; clearTimeout(retryTimer.current)
    setCsrfToken(''); setData(null); setError(new ApiError(401, '请登录。'))
  }
  return <StateContext value={{ data, loading, error, connection, syncedAt, revision, refresh, signOut }}>{children}</StateContext>
}
export function useApp() { const state = useContext(StateContext); if (!state) throw new Error('AppProvider is required'); return state }
