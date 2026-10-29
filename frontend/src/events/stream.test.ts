import { describe, expect, it, vi } from 'vitest'
import { openEventStream } from './stream'

class FakeEventSource {
  static instances: FakeEventSource[] = []
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  listeners = new Map<string, (event: MessageEvent) => void>()
  close = vi.fn()
  constructor(public url: string) { FakeEventSource.instances.push(this) }
  addEventListener(type: string, listener: (event: MessageEvent) => void) { this.listeners.set(type, listener) }
  emit(type: string, data: unknown) { this.listeners.get(type)?.(new MessageEvent(type, { data: JSON.stringify(data) })) }
}

describe('persistent event stream', () => {
  it('keeps material operation status current after leaving the polling page and deduplicates updates', () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    const onEvent = vi.fn(), close = openEventStream(20, onEvent, vi.fn(), vi.fn(), vi.fn())
    const source = FakeEventSource.instances.at(-1)!
    source.emit('material.updated', { seq: 21, type: 'material.updated', operation_id: 'material-1' })
    source.emit('material.updated', { seq: 21, type: 'material.updated', operation_id: 'material-1' })
    expect(onEvent).toHaveBeenCalledOnce()
    expect(onEvent.mock.calls[0][0].operation_id).toBe('material-1')
    close(); vi.unstubAllGlobals()
  })
  it('refreshes confirmed test rules and actual verification results from persisted events', () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    const onEvent = vi.fn(), close = openEventStream(10, onEvent, vi.fn(), vi.fn(), vi.fn())
    const source = FakeEventSource.instances.at(-1)!
    source.emit('test_contract.updated', { seq: 11, type: 'test_contract.updated' })
    source.emit('verification.updated', { seq: 12, type: 'verification.updated' })
    source.emit('verification.updated', { seq: 12, type: 'verification.updated' })
    expect(onEvent.mock.calls.map(([event]) => event.seq)).toEqual([11, 12])
    close(); vi.unstubAllGlobals()
  })
  it('delivers persisted practice-attempt events once without creating a learning-state event', () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    const onEvent = vi.fn(), close = openEventStream(2, onEvent, vi.fn(), vi.fn(), vi.fn())
    const source = FakeEventSource.instances.at(-1)!
    source.emit('quiz.attempted', { seq: 3, type: 'quiz.attempted', operation_id: 'tutor-1' })
    source.emit('quiz.attempted', { seq: 3, type: 'quiz.attempted', operation_id: 'tutor-1' })
    expect(onEvent).toHaveBeenCalledOnce()
    expect(onEvent.mock.calls[0][0].type).toBe('quiz.attempted')
    close(); vi.unstubAllGlobals()
  })
  it('refreshes saved voice metadata and configuration without a second audio connection', () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    const onEvent = vi.fn()
    const close = openEventStream(1, onEvent, vi.fn(), vi.fn(), vi.fn())
    const source = FakeEventSource.instances.at(-1)!
    source.emit('voice.session_updated', { seq: 2, type: 'voice.session_updated' })
    source.emit('voice.session_updated', { seq: 2, type: 'voice.session_updated' })
    source.emit('voice.configured', { seq: 3, type: 'voice.configured' })
    expect(onEvent.mock.calls.map(([event]) => event.seq)).toEqual([2, 3])
    close(); vi.unstubAllGlobals()
  })
  it('starts after the snapshot cursor and ignores duplicate or older events', () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    const onEvent = vi.fn()
    const close = openEventStream(41, onEvent, vi.fn(), vi.fn(), vi.fn())
    const source = FakeEventSource.instances.at(-1)!
    expect(source.url).toBe('/api/v1/events?cursor=41')
    source.emit('business', { seq: 42, type: 'message.created' })
    source.emit('business', { seq: 42, type: 'message.created' })
    source.emit('business', { seq: 40, type: 'task.updated' })
    source.emit('business', { seq: 43, type: 'task.updated' })
    expect(onEvent.mock.calls.map(([event]) => event.seq)).toEqual([42, 43])
    close()
    expect(source.close).toHaveBeenCalledOnce()
    vi.unstubAllGlobals()
  })
  it('requests a fresh snapshot on reconnect and closes before an expired-cursor reset', () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    const state = vi.fn(), reset = vi.fn(), reconnect = vi.fn()
    openEventStream(5, vi.fn(), state, reset, reconnect)
    const source = FakeEventSource.instances.at(-1)!
    source.onopen?.()
    expect(reconnect).not.toHaveBeenCalled()
    source.onerror?.()
    source.onopen?.()
    expect(state.mock.calls.map(([value]) => value)).toEqual(['connecting', 'connected', 'reconnecting', 'connected'])
    expect(reconnect).toHaveBeenCalledTimes(2)
    source.emit('reset_required', {})
    expect(source.close).toHaveBeenCalledOnce()
    expect(reset).toHaveBeenCalledOnce()
    vi.unstubAllGlobals()
  })
  it('accepts named sandbox updates and deduplicates their persisted cursor', () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    const onEvent = vi.fn()
    const close = openEventStream(10, onEvent, vi.fn(), vi.fn(), vi.fn())
    const source = FakeEventSource.instances.at(-1)!
    source.emit('sandbox.execution_updated', { seq: 11, type: 'sandbox.execution_updated' })
    source.emit('sandbox.execution_updated', { seq: 11, type: 'sandbox.execution_updated' })
    expect(onEvent).toHaveBeenCalledOnce()
    expect(onEvent.mock.calls[0][0].seq).toBe(11)
    close()
    vi.unstubAllGlobals()
  })
  it('delivers named coding progress updates without replaying an already-seen cursor', () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    const onEvent = vi.fn()
    const close = openEventStream(20, onEvent, vi.fn(), vi.fn(), vi.fn())
    const source = FakeEventSource.instances.at(-1)!
    source.emit('coding.run_updated', { seq: 21, type: 'coding.run_updated' })
    source.emit('coding.run_updated', { seq: 21, type: 'coding.run_updated' })
    expect(onEvent).toHaveBeenCalledOnce()
    expect(onEvent.mock.calls[0][0].seq).toBe(21)
    close()
    vi.unstubAllGlobals()
  })
})
