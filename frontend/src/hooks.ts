import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, newRequestKey } from './api/client'
import type { GoalDetail } from './api/types'
import { useApp } from './state'

export function useGoal(id: string) {
  const { revision } = useApp()
  const [attempt, setAttempt] = useState(0)
  const [loaded, setLoaded] = useState<{ id: string; detail: GoalDetail } | null>(null)
  const [error, setError] = useState<Error | null>(null)
  useEffect(() => {
    let current = true
    let retry: ReturnType<typeof setTimeout> | undefined
    void api.goal(id).then(value => { if (current) { setLoaded({ id, detail: value }); setError(null) } }).catch(err => { if (current) { setError(err); if (err instanceof ApiError && (err.status === 0 || err.status >= 500)) retry = setTimeout(() => setAttempt(value => value + 1), 3000) } })
    return () => { current = false; clearTimeout(retry) }
  }, [id, revision, attempt])
  return { detail: loaded?.id === id ? loaded.detail : null, error, reload: () => setAttempt(value => value + 1) }
}

/** Keep a command's key across network retries, but not across edited input. */
export function useAction() {
  const { refresh } = useApp()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const previous = useRef<{ signature: string; key: string; action: ((key: string) => Promise<unknown>) | null }>({ signature: '', key: '', action: null })
  const busy = useRef(false)
  const run = useCallback(async <T,>(signature: string, action: (key: string) => Promise<T>): Promise<T | undefined> => {
    if (busy.current) return
    if (previous.current.signature !== signature) previous.current = { signature, key: newRequestKey(), action }
    busy.current = true; setPending(true); setError(null)
    try {
      const result = await previous.current.action!(previous.current.key) as T
      previous.current = { signature: '', key: '', action: null }
      await refresh()
      return result
    } catch (err) {
      setError(err instanceof Error ? err : new Error('操作未完成，请重试。'))
      if (err instanceof ApiError && err.status >= 400 && err.status < 500 && ![408, 429].includes(err.status)) previous.current = { signature: '', key: '', action: null }
      if (err instanceof ApiError && err.status === 409) await refresh()
    }
    finally { setPending(false); busy.current = false }
  }, [refresh])
  return { pending, error, run, clearError: () => setError(null) }
}
