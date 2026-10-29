import { api, newRequestKey } from '../api/client'
import { PcmFrames, PcmResampler } from './audio'
import { openCapture, PcmPlayback } from './browser-audio'
import type { CaptureHandle } from './browser-audio'
import type { VoiceSession, VoiceTranscript } from './types'

interface Callbacks { session: (value: VoiceSession) => void; state: (value: string) => void; transcript: (value: VoiceTranscript) => void; level: (value: number) => void; ready: (canInterrupt: boolean) => void; error: (error: Error) => void; ended: () => void }

export class VoiceBridge {
  private abort = new AbortController()
  private capture: CaptureHandle | null = null
  private playback: PcmPlayback | null = null
  private resampler: PcmResampler | null = null
  private frames: PcmFrames | null = null
  private socket: WebSocket | null = null
  private sessionId: string | null = null
  private closed = false
  private muted = false
  private acknowledged = new Map<string, number>()
  private interrupted = new Set<string>()
  private currentResponse = ''
  private packet: { bytes: number; response: string } | null = null
  private sequence = -1
  private ackTimer: ReturnType<typeof setInterval> | undefined
  private handshakeTimer: ReturnType<typeof setTimeout> | undefined
  private deadlineTimer: ReturnType<typeof setTimeout> | undefined
  private lastLevel = 0
  private maxFrameBytes = 65536
  constructor(private callbacks: Callbacks) {}
  private send(value: unknown) { if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(value)) }
  private fail(error: unknown) {
    if (this.closed) return
    this.callbacks.error(error instanceof Error ? error : new Error('语音连接已中断。'))
    void this.stop()
  }
  async start(conversationId: string, configVersion: number, goalRevision: number | null) {
    try {
      this.callbacks.state('permission')
      this.capture = await openCapture((samples, rate) => {
        if (this.closed) return
        if (performance.now() - this.lastLevel > 100) {
          this.lastLevel = performance.now()
          this.callbacks.level(Math.sqrt(samples.reduce((sum, sample) => sum + sample * sample, 0) / Math.max(1, samples.length)))
        }
        if (!this.muted && this.resampler && this.frames && this.socket?.readyState === WebSocket.OPEN) {
          if (this.socket.bufferedAmount > 262144) { this.fail(new Error('语音网络发送缓冲过大，已停止上传。')); return }
          try { for (const frame of this.frames.push(this.resampler.push(samples))) this.socket.send(frame) }
          catch (error) { this.fail(error) }
        }
        void rate
      }, this.abort.signal, () => this.fail(new Error('麦克风已断开，语音会话已停止。')))
      if (this.closed) { await this.capture.close(); return }
      this.callbacks.state('connecting')
      this.handshakeTimer = setTimeout(() => this.fail(new Error('语音连接等待超时。')), 20000)
      const session = await api.createVoiceSession(conversationId, configVersion, goalRevision, newRequestKey())
      this.sessionId = session.id
      if (this.closed) { await api.stopVoiceSession(session.id, newRequestKey()); return }
      this.callbacks.session(session)
      const ticket = await api.voiceTicket(session.id, newRequestKey())
      if (this.closed) return
      const expected = `/api/v1/voice-sessions/${encodeURIComponent(session.id)}/socket`
      if (ticket.websocket_path !== expected) throw new Error('语音连接地址无效。')
      const socket = this.socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}${expected}`)
      socket.binaryType = 'arraybuffer'
      socket.onopen = () => this.send({ type: 'authenticate', ticket: ticket.ticket })
      socket.onmessage = event => { try { this.receive(event.data) } catch (error) { this.fail(error) } }
      socket.onerror = () => this.fail(new Error('语音连接失败，请检查网络后手动重新开始。'))
      socket.onclose = () => { if (!this.closed) this.fail(new Error('语音连接已断开，已停止麦克风；不会自动重连。')) }
      this.deadlineTimer = setTimeout(() => { void this.stop() }, session.max_seconds * 1000)
      this.ackTimer = setInterval(() => {
        for (const response of this.acknowledged.keys()) {
          const played = this.playback?.playedBytes(response) ?? 0
          if (played > (this.acknowledged.get(response) ?? 0)) {
            this.send({ type: 'playback.ack', response_id: response, played_bytes: played })
            this.acknowledged.set(response, played)
          }
        }
      }, 200)
    } catch (error) { this.fail(error) }
  }
  private receive(data: unknown) {
    if (this.closed) return
    if (data instanceof ArrayBuffer) {
      const packet = this.packet; this.packet = null
      if (!packet || data.byteLength !== packet.bytes || data.byteLength > this.maxFrameBytes || data.byteLength % 2) throw new Error('语音音频帧格式无效。')
      if (!this.interrupted.has(packet.response)) this.playback?.enqueue(packet.response, data)
      return
    }
    if (typeof data !== 'string' || data.length > 65536) throw new Error('语音事件格式无效。')
    const value = JSON.parse(data) as Record<string, unknown>
    if (value.type === 'session.ready') {
      if (!this.capture || this.playback || value.session_id !== this.sessionId || value.channels !== 1 || value.format !== 'pcm_s16le'
        || !Number.isInteger(value.input_sample_rate) || !Number.isInteger(value.output_sample_rate)) throw new Error('语音采样配置无效。')
      const inputRate = value.input_sample_rate as number, outputRate = value.output_sample_rate as number
      if (outputRate < 8000 || outputRate > 192000) throw new Error('语音播放采样率无效。')
      this.maxFrameBytes = typeof value.max_frame_bytes === 'number' && Number.isInteger(value.max_frame_bytes) && value.max_frame_bytes >= 2 ? Math.min(65536, value.max_frame_bytes) : 65536
      this.resampler = new PcmResampler(this.capture.sampleRate, inputRate)
      this.frames = new PcmFrames(inputRate, this.maxFrameBytes)
      this.playback = new PcmPlayback(this.capture.context, outputRate)
      clearTimeout(this.handshakeTimer)
      this.callbacks.ready(value.can_interrupt === true); this.callbacks.state('active')
    } else if (value.type === 'audio.chunk') {
      if (!this.playback || this.packet || typeof value.response_id !== 'string' || !value.response_id
        || !Number.isInteger(value.sequence) || (value.sequence as number) <= this.sequence || !Number.isInteger(value.bytes)
        || (value.bytes as number) < 0 || (value.bytes as number) > this.maxFrameBytes) throw new Error('语音音频顺序无效。')
      this.sequence = value.sequence as number
      if (!this.interrupted.has(value.response_id)) this.currentResponse = value.response_id
      this.packet = { bytes: value.bytes as number, response: value.response_id }
      if (!this.acknowledged.has(value.response_id)) this.acknowledged.set(value.response_id, 0)
    } else if (value.type === 'transcript') {
      if (!Number.isSafeInteger(value.index) || (value.index as number) < 0 || !['user', 'assistant'].includes(value.speaker as string) || typeof value.text !== 'string' || value.text.length > 32000) throw new Error('语音字幕格式无效。')
      this.callbacks.transcript({ index: value.index as number, speaker: value.speaker as VoiceTranscript['speaker'], text: value.text, final: value.final === true, interrupted: value.interrupted === true })
    } else if (value.type === 'session.state') {
      if (value.state === 'interrupted' && typeof value.response_id === 'string') this.interrupt(value.response_id)
      if (typeof value.state === 'string') this.callbacks.state(value.state)
    } else if (value.type === 'session.error') this.fail(new Error(typeof value.message === 'string' ? value.message : '语音服务暂时不可用。'))
    else if (value.type === 'session.ended') { void this.stop(false) }
  }
  mute(value: boolean) { this.muted = value; this.capture?.mute(value) }
  interrupt(response = this.currentResponse) {
    if (!response || this.interrupted.has(response)) return
    const played = this.playback?.playedBytes(response) ?? 0
    this.playback?.clear()
    for (const previous of this.acknowledged.keys()) this.interrupted.add(previous)
    this.interrupted.add(response)
    this.send({ type: 'interrupt', response_id: response, played_bytes: played })
  }
  async stop(remote = true) {
    if (this.closed) return
    this.closed = true; this.abort.abort()
    clearInterval(this.ackTimer); clearTimeout(this.handshakeTimer); clearTimeout(this.deadlineTimer)
    this.playback?.close(); this.callbacks.level(0)
    const socket = this.socket
    if (remote && socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'stop' }))
    if (socket) { socket.onopen = null; socket.onmessage = null; socket.onerror = null; socket.onclose = null; socket.close() }
    await this.capture?.close()
    if (this.sessionId && remote) {
      try { this.callbacks.session(await api.stopVoiceSession(this.sessionId, newRequestKey())) }
      catch (error) { this.callbacks.error(error instanceof Error ? error : new Error('本地已停止，服务端结束状态尚待确认。')) }
    }
    this.callbacks.ended()
  }
}
