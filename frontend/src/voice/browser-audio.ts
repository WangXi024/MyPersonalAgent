import { decodePcm16 } from './audio'

export interface CaptureHandle { context: AudioContext; sampleRate: number; close: () => Promise<void>; mute: (value: boolean) => void }

export function audioSupported() {
  return window.isSecureContext && !!navigator.mediaDevices?.getUserMedia && typeof AudioContext !== 'undefined' && typeof AudioWorkletNode !== 'undefined'
}

export async function openCapture(onSamples: (samples: Float32Array, rate: number) => void, signal: AbortSignal, onEnded?: () => void): Promise<CaptureHandle> {
  if (!audioSupported()) throw new Error('此浏览器环境暂不支持麦克风，请使用 localhost 或 HTTPS 下的现代浏览器。')
  const context = new AudioContext()
  let stream: MediaStream | undefined
  let source: MediaStreamAudioSourceNode | undefined
  let processor: AudioWorkletNode | undefined
  let sink: GainNode | undefined
  let closed = false
  const close = async () => {
    if (closed) return
    closed = true
    stream?.getTracks().forEach(track => track.stop())
    if (processor) processor.port.onmessage = null
    source?.disconnect(); processor?.disconnect(); sink?.disconnect()
    if (context.state !== 'closed') await context.close().catch(() => undefined)
    signal.removeEventListener('abort', abort)
  }
  const abort = () => { void close() }
  signal.addEventListener('abort', abort, { once: true })
  try {
    if (signal.aborted) throw new DOMException('已取消', 'AbortError')
    await context.resume()
    stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }, video: false })
    if (closed || signal.aborted) { stream.getTracks().forEach(track => track.stop()); throw new DOMException('已取消', 'AbortError') }
    stream.getTracks().forEach(track => track.addEventListener('ended', () => { if (!closed) onEnded?.() }))
    await context.audioWorklet.addModule(`${import.meta.env.BASE_URL}voice-capture.worklet.js`)
    if (closed || signal.aborted) throw new DOMException('已取消', 'AbortError')
    source = context.createMediaStreamSource(stream)
    processor = new AudioWorkletNode(context, 'learning-voice-capture')
    sink = context.createGain(); sink.gain.value = 0
    processor.port.onmessage = event => { if (!closed && event.data instanceof Float32Array) onSamples(event.data, context.sampleRate) }
    source.connect(processor); processor.connect(sink); sink.connect(context.destination)
    return { context, sampleRate: context.sampleRate, close, mute: value => stream?.getAudioTracks().forEach(track => { track.enabled = !value }) }
  } catch (error) { await close(); throw error }
}

interface PlaybackSegment { node: AudioBufferSourceNode; response: string; start: number; count: number; rate: number; stopped: boolean; ended: boolean }

/** Counts samples actually traversed by AudioContext time, never network delivery progress. */
export class PcmPlayback {
  private segments: PlaybackSegment[] = []
  private completed = new Map<string, number>()
  private nextTime = 0
  constructor(private context: AudioContext, private sampleRate: number) {}
  enqueue(response: string, bytes: ArrayBuffer) {
    const samples = decodePcm16(bytes)
    if (!samples.length) return
    const now = this.context.currentTime
    if (this.nextTime - now > 60) throw new Error('语音播放缓存过长，请重新开始会话。')
    const buffer = this.context.createBuffer(1, samples.length, this.sampleRate)
    buffer.getChannelData(0).set(samples)
    const node = this.context.createBufferSource(); node.buffer = buffer; node.connect(this.context.destination)
    const segment: PlaybackSegment = { node, response, start: Math.max(now + 0.01, this.nextTime), count: samples.length, rate: this.sampleRate, stopped: false, ended: false }
    this.nextTime = segment.start + samples.length / this.sampleRate
    this.segments.push(segment)
    node.onended = () => {
      segment.ended = true
      node.disconnect()
    }
    node.start(segment.start)
  }
  playedBytes(response: string) {
    const now = this.playbackTime()
    for (const item of this.segments) {
      if (item.ended && !item.stopped && now >= item.start + item.count / item.rate) {
        this.completed.set(item.response, (this.completed.get(item.response) ?? 0) + item.count * 2)
        item.stopped = true
      }
    }
    this.segments = this.segments.filter(item => !item.stopped)
    return (this.completed.get(response) ?? 0) + this.segments.filter(item => item.response === response && !item.stopped)
      .reduce((sum, item) => sum + Math.min(item.count, Math.max(0, Math.floor((now - item.start) * item.rate))) * 2, 0)
  }
  private playbackTime() {
    const output = this.context.getOutputTimestamp?.().contextTime
    if (typeof output === 'number' && Number.isFinite(output) && output >= 0 && output <= this.context.currentTime) return output
    const latency = [this.context.baseLatency, this.context.outputLatency].reduce((sum, value) => sum + (Number.isFinite(value) && value > 0 ? value : 0), 0)
    return Math.max(0, this.context.currentTime - latency)
  }
  clear() {
    const positions = new Map<string, number>()
    for (const item of this.segments) positions.set(item.response, this.playedBytes(item.response))
    for (const [response, bytes] of positions) this.completed.set(response, bytes)
    for (const item of [...this.segments]) {
      item.stopped = true
      try { item.node.stop() } catch { /* An already ended node has no remaining playback. */ }
      item.node.disconnect()
    }
    this.segments = []; this.nextTime = this.context.currentTime
  }
  close() { this.clear(); this.completed.clear() }
}
