import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Mic, MicOff, Square, VolumeX } from 'lucide-react'
import { api, newRequestKey } from '../api/client'
import { useApp } from '../state'
import { ErrorNotice } from './ui'
import { VoiceBridge } from '../voice/bridge'
import { audioSupported, openCapture, PcmPlayback } from '../voice/browser-audio'
import { encodePcm16 } from '../voice/audio'
import { voiceReason } from '../voice/labels'
import type { CaptureHandle } from '../voice/browser-audio'
import type { VoiceCapabilities, VoiceSession, VoiceTranscript } from '../voice/types'
import './voice.css'

const states: Record<string, string> = { permission: '正在等待麦克风授权', connecting: '正在连接语音', active: '语音已连接', speaking: '正在回复', thinking: '正在组织回复', listening: '正在聆听', interrupted: '已打断回复', stopping: '正在结束语音' }
const sessionStates: Record<VoiceSession['status'], string> = { pending: '等待连接', connecting: '正在连接', active: '会话进行中', stopping: '等待结束确认', closed: '会话已结束', failed: '会话未完成', interrupted: '连接已中断', expired: '会话已超时' }

export function VoicePanel({ conversationId, canSend = true, goalRevision = null, onUseTranscript }: {
  conversationId: string | null; canSend?: boolean; goalRevision?: number | null; onUseTranscript: (text: string) => void
}) {
  const { revision } = useApp()
  const [capabilities, setCapabilities] = useState<VoiceCapabilities | null>(null)
  const [session, setSession] = useState<VoiceSession | null>(null)
  const [transcripts, setTranscripts] = useState<VoiceTranscript[]>([])
  const [error, setError] = useState<Error | null>(null)
  const [expanded, setExpanded] = useState(false)
  const [working, setWorking] = useState(false)
  const [state, setState] = useState('')
  const [muted, setMuted] = useState(false)
  const [canInterrupt, setCanInterrupt] = useState(false)
  const [level, setLevel] = useState(0)
  const [localTest, setLocalTest] = useState(false)
  const [notice, setNotice] = useState('')
  const [draft, setDraft] = useState<string | null>(null)
  const bridge = useRef<VoiceBridge | null>(null)
  const localCapture = useRef<CaptureHandle | null>(null)
  const localPlayback = useRef<PcmPlayback | null>(null)
  const localAbort = useRef<AbortController | null>(null)
  const localTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const mounted = useRef(false)
  const busy = useRef(false)
  const generation = useRef(0)
  const sessionRef = useRef<VoiceSession | null>(null)
  const restore = useCallback(async () => {
    const attempt = generation.current
    try {
      const metadata = await api.voiceCapabilities(conversationId ?? undefined)
      if (!mounted.current || generation.current !== attempt) return
      setCapabilities(metadata)
      const previous = metadata.latest_session
      if (previous && (!sessionRef.current || previous.id === sessionRef.current.id || !bridge.current)) {
        sessionRef.current = previous; setSession(previous)
        if (!bridge.current) setTranscripts(previous.transcripts)
      } else if (sessionRef.current) {
        const saved = await api.voiceSession(sessionRef.current.id)
        if (mounted.current && generation.current === attempt) { sessionRef.current = saved; setSession(saved); if (!bridge.current) setTranscripts(saved.transcripts) }
      }
    } catch (error) { if (mounted.current && generation.current === attempt) setError(error instanceof Error ? error : new Error('语音状态暂不可用。')) }
  }, [conversationId])
  const stopLocal = useCallback(() => {
    localAbort.current?.abort(); localAbort.current = null
    clearTimeout(localTimer.current)
    localPlayback.current?.close(); localPlayback.current = null
    void localCapture.current?.close(); localCapture.current = null
    busy.current = false
    if (mounted.current) { setLocalTest(false); setLevel(0) }
  }, [])
  const release = useCallback(() => { generation.current++; void bridge.current?.stop(); bridge.current = null; stopLocal() }, [stopLocal])
  useEffect(() => {
    mounted.current = true
    sessionRef.current = null
    setCapabilities(null); setSession(null); setTranscripts([]); setDraft(null); setWorking(false); setError(null)
    const leave = () => { release(); setWorking(false); setState(''); setCanInterrupt(false) }
    window.addEventListener('pagehide', leave)
    return () => { window.removeEventListener('pagehide', leave); mounted.current = false; release() }
  }, [conversationId, release])
  useEffect(() => { void restore() }, [restore, revision])
  const start = async () => {
    if (busy.current || !conversationId || !canSend || capabilities?.status !== 'ready' || !capabilities.can_start) return
    busy.current = true; const attempt = ++generation.current
    setExpanded(true); setWorking(true); setError(null); setNotice(''); setMuted(false); setCanInterrupt(false); setTranscripts([])
    const current = () => mounted.current && generation.current === attempt
    const connection = new VoiceBridge({
      session: value => { if (current()) { sessionRef.current = value; setSession(value) } },
      state: value => { if (current()) setState(value) },
      transcript: value => { if (current()) setTranscripts(previous => [...previous.filter(item => !(item.index === value.index && item.speaker === value.speaker)), value].sort((a, b) => a.index - b.index)) },
      level: value => { if (current()) setLevel(value) },
      ready: value => { if (current()) setCanInterrupt(value) },
      error: value => { if (current()) setError(value) },
      ended: () => { if (current()) { bridge.current = null; busy.current = false; setWorking(false); setState(''); setCanInterrupt(false); void restore() } },
    })
    bridge.current = connection
    await connection.start(conversationId, capabilities.config.version, goalRevision)
  }
  const stop = async () => {
    setState('stopping')
    if (bridge.current) await bridge.current.stop()
    else if (session) {
      try { const value = await api.stopVoiceSession(session.id, newRequestKey()); sessionRef.current = value; setSession(value); await restore() }
      catch (error) { setError(error instanceof Error ? error : new Error('会话结束状态尚待确认。')) }
      finally { setState('') }
    }
  }
  const testMicrophone = async () => {
    if (busy.current) return
    busy.current = true; setExpanded(true); setLocalTest(true); setError(null); setNotice('正在本机测试麦克风，3 秒后本机回放，不上传音频。')
    const abort = new AbortController(); localAbort.current = abort
    const chunks: Float32Array[] = []; let size = 0
    try {
      const capture = await openCapture((samples, rate) => {
        if (abort.signal.aborted) return
        const block = samples.slice(0, Math.max(0, rate * 3 - size))
        chunks.push(block); size += block.length
        if (mounted.current) setLevel(Math.sqrt(block.reduce((sum, value) => sum + value * value, 0) / Math.max(1, block.length)))
      }, abort.signal, stopLocal)
      localCapture.current = capture
      if (abort.signal.aborted) { await capture.close(); return }
      localTimer.current = setTimeout(() => {
        capture.mute(true)
        if (abort.signal.aborted || !mounted.current) { stopLocal(); return }
        const samples = new Float32Array(size); let position = 0
        for (const chunk of chunks) { samples.set(chunk, position); position += chunk.length }
        chunks.length = 0
        localPlayback.current = new PcmPlayback(capture.context, capture.sampleRate)
        localPlayback.current.enqueue('local-test', encodePcm16(samples)); setNotice('正在本机回放测试录音，音频没有上传。'); setLevel(0)
        localTimer.current = setTimeout(() => { stopLocal(); if (mounted.current) setNotice('本地麦克风测试已结束，音频没有上传。') }, samples.length / capture.sampleRate * 1000 + 150)
      }, 3000)
    } catch (error) { stopLocal(); if (mounted.current) setError(error instanceof Error ? error : new Error('麦克风测试未完成。')) }
  }
  const supported = audioSupported()
  const allowed = canSend && !!conversationId && capabilities?.status === 'ready' && capabilities.can_start && supported
  const selectedModel = working && session ? session.config : capabilities?.config
  return <section className="voice-panel" aria-label="实时语音">
    <div className="voice-controls row wrap"><button type="button" className="text-button" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}><Mic size={14} />语音</button>
      <button type="button" className="text-button" disabled={!allowed || working || localTest} onClick={() => void start()}>开始语音</button>
      {working && <><button type="button" className="text-button" onClick={() => { const value = !muted; bridge.current?.mute(value); setMuted(value) }}>{muted ? <MicOff size={13} /> : <Mic size={13} />}{muted ? '取消静音' : '麦克风静音'}</button><button type="button" className="text-button" disabled={!canInterrupt} onClick={() => bridge.current?.interrupt()}><VolumeX size={13} />打断回复</button></>}
      {(working || session && !session.cleanup_done) && <button type="button" className="text-button" onClick={() => void stop()}><Square size={12} />结束语音</button>}
    </div>
    {expanded && <div className="voice-detail"><p className="voice-note">{working ? states[state] || '语音连接处理中' : capabilities ? voiceReason(capabilities.start_reason || capabilities.reason) : '正在读取语音配置'}{muted && working ? ' · 麦克风静音' : ''}</p>
      {selectedModel?.provider && selectedModel.model_id && <p className="voice-note">语音模型：{capabilities?.providers.find(provider => provider.id === selectedModel.provider)?.name || selectedModel.provider} · {selectedModel.model_id}{selectedModel.voice ? ` · ${selectedModel.voice}` : ''}</p>}
      {!supported && <p className="voice-note">当前环境暂不支持音频采集；请在 localhost 或 HTTPS 下使用支持 AudioWorklet 的浏览器。</p>}
      {capabilities?.can_manage && <Link className="text-button" to="/settings/voice">语音配置</Link>}
      <div className="voice-level"><meter aria-label="本机麦克风电平" min={0} max={1} value={Math.min(1, Math.max(0, level))} /><span>{localTest ? '仅本机测试' : '麦克风电平'}</span></div>
      {localTest ? <button type="button" className="text-button" onClick={() => { stopLocal(); setNotice('本地测试已停止，音频没有上传。') }}>停止本地测试</button> : <button type="button" className="text-button" disabled={!supported || working} onClick={() => void testMicrophone()}>本地测试麦克风</button>}
      {notice && <p className="voice-note" role="status">{notice}</p>}
      <ErrorNotice error={error} />
      {session?.error_message && <p className="voice-note" role="alert">{session.error_message}</p>}
      {session && <p className="voice-note">{sessionStates[session.status]} · 服务端记录 {Math.floor(session.duration_seconds)} 秒 / 最长 {session.max_seconds} 秒{!session.cleanup_done && !working ? ' · 结束状态待确认' : ''}</p>}
      {capabilities && <p className="voice-note">今天可用 {Math.floor(capabilities.budget.available_seconds)} 秒。语音不会自动重连；音频默认不保存。</p>}
      {transcripts.length > 0 && <ol className="voice-transcripts" aria-label="语音转写">{transcripts.map(item => <li key={`${item.index}/${item.speaker}`}><strong>{item.speaker === 'user' ? '你' : '学习伙伴'}{item.interrupted ? ' · 已打断' : ''}{!item.final ? ' · 转写中' : ''}</strong><p>{item.text}</p>{item.final && item.text.trim() && <button type="button" className="text-button" onClick={() => setDraft(item.text)}>编辑这段转写</button>}</li>)}</ol>}
      {draft !== null && <div className="voice-transcript-draft"><label>编辑要发送的转写<textarea rows={3} maxLength={10000} value={draft} onChange={event => setDraft(event.target.value)} /></label><button type="button" className="text-button" disabled={!canSend || !draft.trim()} onClick={() => { onUseTranscript(draft.trim()); setDraft(null); setNotice('转写已追加到消息框，请检查后再发送。') }}>追加到消息框</button></div>}
    </div>}
  </section>
}
