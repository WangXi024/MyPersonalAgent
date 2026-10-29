import { useEffect, useState } from 'react'
import { api } from './api/client'
import type { VerificationData } from './api/verification'
import { useApp } from './state'
const empty: VerificationData = { contracts: [], runs: [] }
export function useVerification(goalId: string) {
  const { revision } = useApp()
  const [attempt, setAttempt] = useState(0)
  const [loaded, setLoaded] = useState<{ goalId: string; data: VerificationData } | null>(null)
  const [error, setError] = useState<Error | null>(null)
  useEffect(() => {
    let current = true
    void api.verification(goalId).then(data => { if (current) { setLoaded({ goalId, data }); setError(null) } }).catch(error => { if (current) setError(error instanceof Error ? error : new Error('程序验证记录暂不可用。')) })
    return () => { current = false }
  }, [goalId, revision, attempt])
  return { data: loaded?.goalId === goalId ? loaded.data : empty, ready: loaded?.goalId === goalId, error, reload: () => setAttempt(value => value + 1) }
}
