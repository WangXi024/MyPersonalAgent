import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../api/client'
import { bootstrap } from '../test/fixtures'
import { voiceCapabilities, voiceConfig } from '../test/voice-fixtures'
import { VoiceSettings } from './VoiceSettings'

let data = structuredClone(bootstrap)
const refresh = vi.fn().mockResolvedValue(undefined)
vi.mock('../state', () => ({ useApp: () => ({ data, revision: 1, refresh }) }))
beforeEach(() => {
  data = structuredClone(bootstrap); data.user.can_manage_models = true
  vi.spyOn(api, 'voiceConfig').mockResolvedValue(voiceConfig)
  vi.spyOn(api, 'voiceCapabilities').mockResolvedValue(voiceCapabilities)
  vi.spyOn(api, 'saveVoiceConfig').mockResolvedValue({ ...voiceConfig, version: 2 })
  vi.spyOn(api, 'createVoiceSession').mockResolvedValue(undefined as never)
})
function mount() { return render(<MemoryRouter><VoiceSettings /></MemoryRouter>) }
describe('voice configuration without automatic supplier traffic', () => {
  it('reloads the current version after a conflict and requires a new explicit save', async () => {
    const user = userEvent.setup()
    vi.mocked(api.saveVoiceConfig).mockRejectedValueOnce(new ApiError(409, '配置已变化'))
    vi.mocked(api.voiceConfig).mockResolvedValueOnce(voiceConfig).mockResolvedValue({ ...voiceConfig, version: 4 })
    mount()
    await user.click(await screen.findByRole('button', { name: '保存语音配置' }))
    await user.click(await screen.findByRole('button', { name: '读取最新配置' }))
    await screen.findByLabelText('语音供应商')
    expect(api.saveVoiceConfig).toHaveBeenCalledOnce()
    await user.click(screen.getByRole('button', { name: '保存语音配置' }))
    expect(vi.mocked(api.saveVoiceConfig).mock.calls[1][0].expected_version).toBe(4)
    expect(api.createVoiceSession).not.toHaveBeenCalled()
  })
  it('saves a null disabled provider draft without credentials or a supplier connection', async () => {
    const user = userEvent.setup()
    mount()
    expect(await screen.findByLabelText('语音供应商')).toHaveValue('')
    expect(screen.getByRole('checkbox', { name: '启用实时语音' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '保存语音配置' }))
    await waitFor(() => expect(api.saveVoiceConfig).toHaveBeenCalledOnce())
    expect(api.saveVoiceConfig).toHaveBeenCalledWith({ expected_version: 1, provider: null, model_id: null, voice: null, endpoint: null, api_version: null, enabled: false, api_key: undefined }, expect.any(String))
    expect(api.createVoiceSession).not.toHaveBeenCalled()
    expect(await screen.findByRole('status')).toHaveTextContent('只有明确开始语音时才建立会话')
  })
  it('keeps the API key write-only, clears it after save and never persists it to browser storage', async () => {
    const user = userEvent.setup(), storage = vi.spyOn(Storage.prototype, 'setItem')
    mount()
    await user.selectOptions(await screen.findByLabelText('语音供应商'), 'openai')
    await user.type(screen.getByLabelText('语音模型 ID'), 'test-realtime-model')
    const key = screen.getByLabelText('语音 API Key')
    expect(key).toHaveAttribute('type', 'password')
    await user.type(key, 'synthetic-test-secret')
    await user.click(screen.getByRole('button', { name: '保存语音配置' }))
    await waitFor(() => expect(key).toHaveValue(''))
    expect(vi.mocked(api.saveVoiceConfig).mock.calls[0][0]).toMatchObject({ api_key: 'synthetic-test-secret', enabled: false })
    expect(storage).not.toHaveBeenCalled(); expect(api.createVoiceSession).not.toHaveBeenCalled()
    expect(screen.queryByText('synthetic-test-secret')).not.toBeInTheDocument()
  })
  it('denies non-admin access without reading the sensitive configuration', () => {
    data.user.can_manage_models = false
    mount()
    expect(screen.getByText('当前账号没有语音配置权限。')).toBeVisible()
    expect(api.voiceConfig).not.toHaveBeenCalled(); expect(api.voiceCapabilities).not.toHaveBeenCalled()
  })
})
