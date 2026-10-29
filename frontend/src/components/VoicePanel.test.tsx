import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { voiceCapabilities, voiceSession } from '../test/voice-fixtures'
import type { VoiceCapabilities, VoiceTranscript } from '../voice/types'
import { VoicePanel } from './VoicePanel'

const media = vi.hoisted(() => ({ start: vi.fn().mockResolvedValue(undefined), stop: vi.fn().mockResolvedValue(undefined), open: vi.fn(), captureClose: vi.fn().mockResolvedValue(undefined), playbackClose: vi.fn(), enqueue: vi.fn(), callbacks: null as { ended: () => void; transcript: (value: VoiceTranscript) => void } | null }))
let revision = 1
vi.mock('../state', () => ({ useApp: () => ({ revision }) }))
vi.mock('../voice/browser-audio', () => ({ audioSupported: () => true, openCapture: media.open, PcmPlayback: class { enqueue = media.enqueue; close = media.playbackClose } }))
vi.mock('../voice/bridge', () => ({ VoiceBridge: class { constructor(callbacks: NonNullable<typeof media.callbacks>) { media.callbacks = callbacks }; start = media.start; stop = media.stop; mute = vi.fn(); interrupt = vi.fn() } }))
beforeEach(() => {
  vi.clearAllMocks(); revision = 1
  vi.spyOn(api, 'voiceCapabilities').mockResolvedValue(voiceCapabilities)
  vi.spyOn(api, 'createVoiceSession').mockResolvedValue(voiceSession)
  vi.spyOn(api, 'stopVoiceSession').mockResolvedValue({ ...voiceSession, status: 'closed', cleanup_done: true })
  media.open.mockImplementation(async (_samples, signal: AbortSignal) => {
    signal.addEventListener('abort', () => { void media.captureClose() })
    return { context: {}, sampleRate: 48000, close: media.captureClose, mute: vi.fn() }
  })
})
afterEach(() => { vi.useRealTimers() })
function mount(onUseTranscript = vi.fn()) { return render(<MemoryRouter><VoicePanel conversationId="conversation-1" onUseTranscript={onUseTranscript} /></MemoryRouter>) }
describe('explicit voice controls', () => {
  it('discards a late saved-state response from the conversation that has already been left', async () => {
    let old: (value: VoiceCapabilities) => void = () => undefined
    vi.mocked(api.voiceCapabilities).mockImplementation(id => id === 'conversation-1' ? new Promise(resolve => { old = resolve }) : Promise.resolve(voiceCapabilities))
    const view = mount()
    view.rerender(<MemoryRouter><VoicePanel conversationId="conversation-2" onUseTranscript={vi.fn()} /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: '语音' }))
    await act(async () => { old({ ...voiceCapabilities, latest_session: { ...voiceSession, transcripts: [{ index: 1, speaker: 'user', text: '旧对话的字幕', final: true, interrupted: false }] } }) })
    expect(screen.queryByText('旧对话的字幕')).not.toBeInTheDocument()
    expect(media.start).not.toHaveBeenCalled()
  })
  it('leaves unconfigured voice disabled, while a local microphone test creates no server session', async () => {
    const user = userEvent.setup()
    mount()
    await waitFor(() => expect(api.voiceCapabilities).toHaveBeenCalledWith('conversation-1'))
    expect(screen.getByRole('button', { name: '开始语音' })).toBeDisabled()
    expect(media.open).not.toHaveBeenCalled(); expect(media.start).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '语音' }))
    await user.click(screen.getByRole('button', { name: '本地测试麦克风' }))
    expect(media.open).toHaveBeenCalledOnce()
    expect(api.createVoiceSession).not.toHaveBeenCalled(); expect(media.start).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '停止本地测试' }))
    expect(media.captureClose).toHaveBeenCalled()
    expect(screen.getByRole('status')).toHaveTextContent('音频没有上传')
  })
  it('restores saved subtitles after refresh, keeps text escaped, and fills the composer only after editing and clicking', async () => {
    const user = userEvent.setup(), useText = vi.fn()
    vi.mocked(api.voiceCapabilities).mockResolvedValue({ ...voiceCapabilities, latest_session: { ...voiceSession, status: 'closed', cleanup_done: true,
      transcripts: [{ index: 1, speaker: 'user', text: '<img src=x onerror=alert(1)>我想学习 Python', final: true, interrupted: false }] } })
    const view = mount(useText)
    await user.click(screen.getByRole('button', { name: '语音' }))
    expect(await screen.findByText('<img src=x onerror=alert(1)>我想学习 Python')).toBeVisible()
    expect(view.container.querySelector('img')).toBeNull()
    expect(media.start).not.toHaveBeenCalled(); expect(api.createVoiceSession).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '编辑这段转写' }))
    const editor = screen.getByLabelText('编辑要发送的转写')
    await user.clear(editor)
    expect(editor).toBeVisible(); expect(screen.getByRole('button', { name: '追加到消息框' })).toBeDisabled()
    await user.type(editor, '请解释 Python 函数')
    expect(useText).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '追加到消息框' }))
    expect(useText).toHaveBeenCalledWith('请解释 Python 函数')
    expect(api.createVoiceSession).not.toHaveBeenCalled()
  })
  it('starts only with an explicit click and releases the bridge when leaving the conversation', async () => {
    const user = userEvent.setup()
    vi.mocked(api.voiceCapabilities).mockResolvedValue({ ...voiceCapabilities, status: 'ready', can_start: true })
    const view = mount()
    await waitFor(() => expect(screen.getByRole('button', { name: '开始语音' })).toBeEnabled())
    expect(media.start).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '开始语音' }))
    expect(media.start).toHaveBeenCalledWith('conversation-1', 1, null)
    expect(screen.getByRole('button', { name: '开始语音' })).toBeDisabled()
    view.unmount()
    expect(media.stop).toHaveBeenCalledOnce()
  })
  it('cleans up a pending local capture after unmount and never uploads or opens a session', async () => {
    const view = mount()
    fireEvent.click(screen.getByRole('button', { name: '语音' }))
    let finish: (value: unknown) => void = () => undefined
    const pending: { signal: AbortSignal | null } = { signal: null }
    media.open.mockImplementation((_samples, value: AbortSignal) => { pending.signal = value; return new Promise(resolve => { finish = resolve }) })
    fireEvent.click(screen.getByRole('button', { name: '本地测试麦克风' }))
    view.unmount()
    expect(api.createVoiceSession).not.toHaveBeenCalled()
    expect(pending.signal?.aborted).toBe(true)
    await act(async () => { finish({ context: {}, sampleRate: 48000, close: media.captureClose, mute: vi.fn() }) })
    expect(media.captureClose).toHaveBeenCalledOnce()
  })
  it('updates persisted final subtitles on an SSE-driven snapshot refresh without reconnecting', async () => {
    const user = userEvent.setup(), view = mount()
    await user.click(screen.getByRole('button', { name: '语音' }))
    vi.mocked(api.voiceCapabilities).mockResolvedValue({ ...voiceCapabilities, latest_session: { ...voiceSession, status: 'interrupted', cleanup_done: true,
      transcripts: [{ index: 4, speaker: 'assistant', text: '断线前已保存的回复', final: true, interrupted: true }] } })
    revision++
    view.rerender(<MemoryRouter><VoicePanel conversationId="conversation-1" onUseTranscript={vi.fn()} /></MemoryRouter>)
    expect(await screen.findByText('断线前已保存的回复')).toBeVisible()
    expect(media.start).not.toHaveBeenCalled()
  })
})
