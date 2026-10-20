import { useCallback, useEffect, useRef, useState } from 'react'
import { api, newRequestKey } from '../api/client'
import { useApp } from '../state'
import { Conversation } from './Conversation'

export function AssistantChat({ compact = false, workspace = false }: { compact?: boolean; workspace?: boolean }) {
  const { data } = useApp()
  const existingId = data?.conversations.find(conversation => conversation.goal_id === null)?.id
  const [createdId, setCreatedId] = useState<string | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const key = useRef(newRequestKey())
  const inFlight = useRef(false)
  const connect = useCallback(async () => {
    if (existingId || createdId || inFlight.current) return
    inFlight.current = true; setError(null)
    try { const conversation = await api.assistantConversation(key.current); setCreatedId(conversation.id) }
    catch (err) { setError(err instanceof Error ? err : new Error('暂时无法连接对话。')) }
    finally { inFlight.current = false }
  }, [existingId, createdId])
  useEffect(() => { void connect() }, [connect])
  return <Conversation conversationId={existingId ?? createdId} operations={data?.operations.filter(operation => operation.goal_id === null && operation.kind === 'tutor') ?? []} compact={compact} workspace={workspace} connectionError={error} retryConnect={() => void connect()} />
}
