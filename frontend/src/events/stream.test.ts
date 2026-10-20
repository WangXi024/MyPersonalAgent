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
})
