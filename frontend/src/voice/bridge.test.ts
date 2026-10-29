import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { voiceSession } from '../test/voice-fixtures'
import { VoiceBridge } from './bridge'

const audio = vi.hoisted(() => ({ open: vi.fn(), close: vi.fn().mockResolvedValue(undefined), mute: vi.fn(), enqueue: vi.fn(), clear: vi.fn(), playbackClose: vi.fn(), played: 0 }))
vi.mock('./browser-audio', () => ({
  openCapture: audio.open,
  PcmPlayback: class { enqueue = audio.enqueue; clear = audio.clear; close = audio.playbackClose; playedBytes() { return audio.played } },
}))
class Socket {
  static OPEN = 1
  static instances: Socket[] = []
  readyState = 1; bufferedAmount = 0; binaryType = ''
  onopen: (() => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: (() => void) | null = null
  send = vi.fn(); close = vi.fn()
  constructor(public url: string) { Socket.instances.push(this) }
  event(value: unknown) { this.onmessage?.({ data: JSON.stringify(value) }) }
  bytes(length: number) { this.onmessage?.({ data: new ArrayBuffer(length) }) }
}
const callbacks = () => ({ session: vi.fn(), state: vi.fn(), transcript: vi.fn(), level: vi.fn(), ready: vi.fn(), error: vi.fn(), ended: vi.fn() })
let captureSamples: (samples: Float32Array, rate: number) => void
const ready = { type: 'session.ready', session_id: 'voice-1', input_sample_rate: 16000, output_sample_rate: 24000, channels: 1, format: 'pcm_s16le', can_interrupt: true, max_frame_bytes: 65536 }
beforeEach(() => {
  vi.useFakeTimers(); vi.clearAllMocks(); audio.played = 0; Socket.instances = []
  vi.stubGlobal('WebSocket', Socket)
  audio.open.mockImplementation(async (onSamples: typeof captureSamples) => { captureSamples = onSamples; return { context: {}, sampleRate: 48000, close: audio.close, mute: audio.mute } })
  vi.spyOn(api, 'createVoiceSession').mockResolvedValue(voiceSession)
  vi.spyOn(api, 'voiceTicket').mockResolvedValue({ ticket: 'one-use-ticket', expires_at: voiceSession.created_at, websocket_path: '/api/v1/voice-sessions/voice-1/socket' })
  vi.spyOn(api, 'stopVoiceSession').mockResolvedValue({ ...voiceSession, status: 'closed', cleanup_done: true })
})
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })
async function connect() {
  const events = callbacks(), bridge = new VoiceBridge(events)
  await bridge.start('conversation-1', 4, 2)
  const socket = Socket.instances[0]; socket.onopen?.(); socket.event(ready)
  return { bridge, socket, events }
}
describe('same-origin realtime bridge', () => {
  it('releases capture when session creation stalls and closes a reservation that arrives after timeout', async () => {
    let finish: (value: typeof voiceSession) => void = () => undefined
    vi.mocked(api.createVoiceSession).mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const events = callbacks(), bridge = new VoiceBridge(events)
    const connecting = bridge.start('conversation-1', 1, null)
    await Promise.resolve(); await Promise.resolve()
    await vi.advanceTimersByTimeAsync(20000)
    expect(events.error).toHaveBeenCalledOnce(); expect(audio.close).toHaveBeenCalledOnce()
    expect(Socket.instances).toHaveLength(0)
    finish(voiceSession); await connecting
    expect(api.stopVoiceSession).toHaveBeenCalledOnce(); expect(api.voiceTicket).not.toHaveBeenCalled()
  })
  it('connects only on explicit start, authenticates outside the URL, and uploads negotiated PCM frames', async () => {
    const events = callbacks(), bridge = new VoiceBridge(events)
    expect(api.createVoiceSession).not.toHaveBeenCalled()
    await bridge.start('conversation-1', 4, 2)
    const socket = Socket.instances[0]
    expect(api.createVoiceSession).toHaveBeenCalledWith('conversation-1', 4, 2, expect.any(String))
    expect(socket.url).toBe('ws://localhost:3000/api/v1/voice-sessions/voice-1/socket')
    expect(socket.url).not.toContain('ticket')
    socket.onopen?.()
    expect(JSON.parse(socket.send.mock.calls[0][0])).toEqual({ type: 'authenticate', ticket: 'one-use-ticket' })
    captureSamples(new Float32Array(4800), 48000)
    expect(socket.send.mock.calls).toHaveLength(1)
    socket.event(ready)
    captureSamples(new Float32Array(4803), 48000)
    const frame = socket.send.mock.calls.at(-1)![0] as ArrayBuffer
    expect(frame).toBeInstanceOf(ArrayBuffer); expect(frame.byteLength).toBe(3200)
    await bridge.stop()
    expect(audio.close).toHaveBeenCalledOnce(); expect(socket.close).toHaveBeenCalledOnce()
  })
  it('ACKs rendered samples only, interrupts once and drops late audio from that response', async () => {
    const { bridge, socket } = await connect()
    socket.event({ type: 'audio.chunk', sequence: 0, bytes: 4800, response_id: 'answer-1' }); socket.bytes(4800)
    await vi.advanceTimersByTimeAsync(200)
    expect(socket.send.mock.calls.some(([value]) => typeof value === 'string' && value.includes('playback.ack'))).toBe(false)
    audio.played = 1200; await vi.advanceTimersByTimeAsync(200)
    expect(JSON.parse(socket.send.mock.calls.at(-1)![0])).toEqual({ type: 'playback.ack', response_id: 'answer-1', played_bytes: 1200 })
    socket.event({ type: 'session.state', state: 'interrupted', response_id: 'answer-1' })
    expect(audio.clear).toHaveBeenCalledOnce()
    expect(JSON.parse(socket.send.mock.calls.at(-1)![0])).toEqual({ type: 'interrupt', response_id: 'answer-1', played_bytes: 1200 })
    socket.event({ type: 'session.state', state: 'interrupted', response_id: 'answer-1' })
    expect(audio.clear).toHaveBeenCalledOnce()
    socket.event({ type: 'audio.chunk', sequence: 1, bytes: 4800, response_id: 'answer-1' }); socket.bytes(4800)
    expect(audio.enqueue).toHaveBeenCalledOnce()
    socket.event({ type: 'audio.chunk', sequence: 2, bytes: 4800, response_id: 'answer-2' }); socket.bytes(4800)
    expect(audio.enqueue).toHaveBeenCalledTimes(2)
    await bridge.stop()
  })
  it('closes tracks, queued playback and connection after disconnect, without reconnecting or new billing requests', async () => {
    const { socket, events } = await connect()
    socket.onclose?.()
    await vi.advanceTimersByTimeAsync(30000)
    expect(events.error).toHaveBeenCalledOnce()
    expect(audio.close).toHaveBeenCalledOnce(); expect(audio.playbackClose).toHaveBeenCalledOnce()
    expect(api.stopVoiceSession).toHaveBeenCalledOnce(); expect(events.ended).toHaveBeenCalledOnce()
    expect(Socket.instances).toHaveLength(1); expect(api.createVoiceSession).toHaveBeenCalledOnce()
  })
  it('refuses cross-origin ticket paths before constructing any socket and still stops the reservation', async () => {
    vi.mocked(api.voiceTicket).mockResolvedValue({ ticket: 'unused', expires_at: '', websocket_path: 'wss://external.invalid/voice-1' })
    const events = callbacks(), bridge = new VoiceBridge(events)
    await bridge.start('conversation-1', 1, null)
    await vi.runAllTimersAsync()
    expect(Socket.instances).toHaveLength(0); expect(events.error).toHaveBeenCalledOnce()
    expect(api.stopVoiceSession).toHaveBeenCalledOnce(); expect(audio.close).toHaveBeenCalledOnce()
  })
  it('rejects an unexpected binary frame and never plays unbound or malformed audio', async () => {
    const { socket, events } = await connect()
    socket.bytes(4)
    await vi.runAllTimersAsync()
    expect(events.error).toHaveBeenCalledOnce(); expect(audio.enqueue).not.toHaveBeenCalled()
  })
})
