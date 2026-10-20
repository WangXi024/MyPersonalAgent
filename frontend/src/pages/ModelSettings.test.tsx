import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, it, vi } from 'vitest'
import { api, ApiError } from '../api/client'
import type { ModelConfig, ModelConfigs } from '../api/types'
import { bootstrap } from '../test/fixtures'
import { ModelSettings } from './ModelSettings'

let data = structuredClone(bootstrap)
const refresh = vi.fn().mockResolvedValue(undefined)
vi.mock('../state', () => ({ useApp: () => ({ data, revision: 1, refresh }) }))
const model: ModelConfig = {
  id: 'configured-model', name: '学习模型', provider: 'bailian', model_id: 'test-model',
  api_base: 'https://dashscope.aliyuncs.com/compatible-mode/v1', input_cny_per_million: '12',
  output_cny_per_million: '36', usd_cny_rate: '7', enabled: false, version: 1,
  key_configured: true, created_at: '2026-10-20T01:00:00Z', test_result: null,
}
let catalogue: ModelConfigs
beforeEach(() => {
  data = structuredClone(bootstrap)
  data.user.can_manage_models = true
  catalogue = { items: [structuredClone(model)], presets: [{ id: 'learning-agent', name: 'test-model', provider: 'bailian', api_base: model.api_base, input_cny_per_million: '12', output_cny_per_million: '36', usd_cny_rate: '7' }] }
  vi.spyOn(api, 'modelConfigs').mockImplementation(async () => catalogue)
  vi.spyOn(api, 'testModelConfig').mockResolvedValue(model)
  vi.spyOn(api, 'saveModelConfig').mockResolvedValue(model)
})
function mount() { return render(<MemoryRouter><ModelSettings /></MemoryRouter>) }

it('blocks non-admin access without loading configuration data', () => {
  data.user.can_manage_models = false
  mount()
  expect(screen.getByText('当前账号没有模型管理权限。')).toBeInTheDocument()
  expect(api.modelConfigs).not.toHaveBeenCalled()
  expect(screen.queryByRole('button', { name: '新增模型' })).not.toBeInTheDocument()
})

it('copies a preset without exposing or submitting its existing secret and does not automatically test', async () => {
  const user = userEvent.setup()
  mount()
  await user.click(await screen.findByRole('button', { name: '复制为可编辑配置' }))
  expect(screen.getByLabelText('API Key', { exact: false })).toHaveValue('')
  expect(screen.getByLabelText('输入价格（元 / 百万 Token）')).toHaveValue(12)
  await user.click(screen.getByRole('button', { name: '保存配置' }))
  await waitFor(() => expect(api.saveModelConfig).toHaveBeenCalledOnce())
  expect(vi.mocked(api.saveModelConfig).mock.calls[0][1]).toMatchObject({ api_key: undefined, source_model_id: 'learning-agent', expected_version: 0 })
  expect(api.testModelConfig).not.toHaveBeenCalled()
  expect(await screen.findByRole('status')).toHaveTextContent('通过连接测试并启用')
})

it('requires a manual test confirmation before inference and then allows enabling', async () => {
  const user = userEvent.setup()
  vi.mocked(api.testModelConfig).mockImplementation(async () => {
    const tested: ModelConfig = { ...model, test_result: { status: 'passed', message: '中文和结构化工具调用已通过', at: model.created_at, estimated_cost_cny: '0.001560' } }
    catalogue = { ...catalogue, items: [tested] }
    return tested
  })
  const enable = vi.spyOn(api, 'setModelEnabled').mockResolvedValue({ ...model, version: 2, enabled: true })
  mount()
  expect(await screen.findByRole('button', { name: '启用' })).toBeDisabled()
  await user.click(screen.getByRole('button', { name: '测试连接' }))
  expect(api.testModelConfig).not.toHaveBeenCalled()
  expect(screen.getByText(/最多生成 512 个 Token/)).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: '开始连接测试' }))
  expect(api.testModelConfig).toHaveBeenCalledWith(model.id, model.version, expect.any(String))
  expect(await screen.findByText(/本次估算 ¥0.001560/)).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: '启用' }))
  expect(enable).toHaveBeenCalledWith(model.id, true, 1, expect.any(String))
})

it('keeps edits on a failed save, reuses the request key, and omits an unchanged key', async () => {
  const user = userEvent.setup()
  vi.mocked(api.saveModelConfig).mockRejectedValueOnce(new ApiError(503, '网关暂不可用')).mockResolvedValue(model)
  mount()
  await user.click(await screen.findByRole('button', { name: '编辑' }))
  await user.clear(screen.getByLabelText('显示名称'))
  await user.type(screen.getByLabelText('显示名称'), '修改后的名称')
  expect(screen.getByLabelText('API Key', { exact: false })).toHaveValue('')
  await user.click(screen.getByRole('button', { name: '保存配置' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('网关暂不可用')
  expect(screen.getByLabelText('显示名称')).toHaveValue('修改后的名称')
  await user.click(screen.getByRole('button', { name: '保存配置' }))
  await waitFor(() => expect(screen.queryByRole('button', { name: '保存配置' })).not.toBeInTheDocument())
  const calls = vi.mocked(api.saveModelConfig).mock.calls
  expect(calls[0][0]).toBe(model.id)
  expect(calls[0][1]).toMatchObject({ name: '修改后的名称', api_key: undefined, expected_version: 1 })
  expect(calls[0][2]).toBe(calls[1][2])
})

it('uses the selected provider endpoint and submits a new key only on save', async () => {
  const user = userEvent.setup()
  mount()
  await screen.findByText('学习模型')
  await user.click(screen.getByRole('button', { name: '新增模型' }))
  await user.selectOptions(screen.getByLabelText('供应商'), 'ark')
  expect(screen.getByLabelText('API 地址', { exact: false })).toHaveValue('https://ark.cn-beijing.volces.com/api/v3')
  await user.type(screen.getByLabelText('显示名称'), '方舟测试')
  await user.type(screen.getByLabelText('模型 ID'), 'test-endpoint')
  await user.type(screen.getByLabelText('API Key', { exact: false }), 'fake-key-for-test-only')
  await user.type(screen.getByLabelText('输入价格（元 / 百万 Token）'), '1')
  await user.type(screen.getByLabelText('输出价格（元 / 百万 Token）'), '2')
  expect(api.saveModelConfig).not.toHaveBeenCalled()
  await user.click(screen.getByRole('button', { name: '保存配置' }))
  await waitFor(() => expect(api.saveModelConfig).toHaveBeenCalledOnce())
  expect(vi.mocked(api.saveModelConfig).mock.calls[0][1]).toMatchObject({ provider: 'ark', api_key: 'fake-key-for-test-only', input_cny_per_million: '1', output_cny_per_million: '2' })
  expect(api.testModelConfig).not.toHaveBeenCalled()
  expect(localStorage.length).toBe(0)
})
