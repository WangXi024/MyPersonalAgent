import type { Activity } from '../api/types'
export type ConnectionState = 'connecting' | 'connected' | 'reconnecting'
const eventTypes = ['business', 'business_event', 'operation.accepted', 'operation.started', 'operation.stage_changed', 'operation.succeeded', 'operation.failed', 'operation.cancelled', 'operation.superseded', 'plan.proposed', 'plan.activated', 'plan.rejected', 'task.recommended', 'task.updated', 'submission.saved', 'evaluation.completed', 'message.created', 'conversation.read', 'conversation.model_changed', 'model.configured', 'goal.created', 'goal.paused', 'goal.resumed', 'goal.cancelled', 'goal.completed', 'settings.updated', 'memory.sync_changed', 'memory.updated', 'memory.deleted']

/** One persisted event stream for the whole authenticated application. */
export function openEventStream(cursor: number, onEvent: (event: Activity) => void, onState: (state: ConnectionState) => void, onReset: () => void, onReconnect: () => void) {
  let lastSeq = cursor
  let opened = false
  const source = new EventSource(`/api/v1/events?cursor=${cursor}`, { withCredentials: true })
  onState('connecting')
  source.onopen = () => { onState('connected'); if (opened) onReconnect(); opened = true }
  source.onerror = () => { onState('reconnecting'); onReconnect() }
  const receive = (message: MessageEvent<string>) => {
    let event: Activity
    try { event = JSON.parse(message.data) as Activity } catch { return }
    if (!Number.isSafeInteger(event.seq) || event.seq <= lastSeq) return
    lastSeq = event.seq
    onEvent(event)
  }
  source.onmessage = receive
  eventTypes.forEach(type => source.addEventListener(type, receive as EventListener))
  source.addEventListener('session_expired', () => { source.close(); window.dispatchEvent(new Event('session-expired')) })
  source.addEventListener('reset_required', () => { source.close(); onReset() })
  return () => source.close()
}
