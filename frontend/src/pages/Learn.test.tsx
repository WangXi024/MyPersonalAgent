import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../api/client'
import { bootstrap, conversationModels, detail, task } from '../test/fixtures'
import { Learn } from './Learn'
const refresh = vi.fn().mockResolvedValue(undefined)
vi.mock('../state', () => ({ useApp: () => ({ data: bootstrap, revision: 1, refresh }) }))
vi.mock('../hooks', async importOriginal => { const original = await importOriginal<typeof import('../hooks')>(); return { ...original, useGoal: () => ({ detail, error: null }) } })

beforeEach(() => {
  detail.tasks = [task]
  vi.spyOn(api, 'messages').mockResolvedValue({ items: [], last_seq: 0, read_seq: 0, has_more: false })
  vi.spyOn(api, 'conversationModels').mockResolvedValue(conversationModels)
  vi.spyOn(api, 'conversationUsage').mockResolvedValue({ requests: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0, usage_complete: true, estimated_cost_cny: '0.000000', cost_complete: true, pricing_known: true })
  vi.stubGlobal('IntersectionObserver', class { observe() {} disconnect() {} })
})
function mount() { return render(<MemoryRouter initialEntries={['/goals/goal-1/learn?task=task-1']}><Routes><Route path="/goals/:goalId/learn" element={<Learn />} /></Routes></MemoryRouter>) }
describe('learning space', () => {
  it('keeps a failed message in the composer and reuses its request key', async () => {
    const command = vi.spyOn(api, 'command').mockRejectedValueOnce(new ApiError(0, '连接中断')).mockResolvedValue({ operation_id: 'op-1', status: 'accepted' })
    const user = userEvent.setup()
    mount()
    const input = screen.getByLabelText('给学习伙伴发消息')
    await user.type(input, '返回值和打印有什么区别？')
    await user.click(screen.getByRole('button', { name: '发送消息' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('连接中断')
    expect(input).toHaveValue('返回值和打印有什么区别？')
    await user.click(screen.getByRole('button', { name: '发送消息' }))
    await waitFor(() => expect(input).toHaveValue(''))
    expect(command.mock.calls[0][2]).toBe(command.mock.calls[1][2])
    expect(command.mock.calls[0][0]).toBe('/conversations/conversation-1/messages')
  })
  it('keeps failed work and spent time, then reports saved rather than passed', async () => {
    const command = vi.spyOn(api, 'command').mockRejectedValueOnce(new ApiError(503, '稍后再试')).mockResolvedValue({ operation_id: 'op-2', status: 'accepted', resource_id: 'submission-1' })
    const user = userEvent.setup()
    mount()
    const input = screen.getByLabelText('答案、代码或学习成果')
    const minutes = screen.getByRole('spinbutton', { name: /本次实际投入/ })
    await user.type(input, 'def add(a, b): return a + b')
    await user.type(minutes, '12')
    await user.click(screen.getByRole('button', { name: '保存并请求评估' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('稍后再试')
    expect(input).toHaveValue('def add(a, b): return a + b')
    expect(minutes).toHaveValue(12)
    await user.click(screen.getByRole('button', { name: '保存并请求评估' }))
    expect(await screen.findByRole('status')).toHaveTextContent('提交已保存，正在等待评估')
    expect(command.mock.calls[0][2]).toBe(command.mock.calls[1][2])
    expect(command.mock.calls[0][1]).toEqual(command.mock.calls[1][1])
    expect(screen.queryByText('符合本次要求')).not.toBeInTheDocument()
  })
  it('keeps separate drafts when changing between tasks', async () => {
    detail.tasks = [task, { ...task, id: 'task-2', title: '练习参数' }]
    const user = userEvent.setup()
    mount()
    await user.type(screen.getByLabelText('答案、代码或学习成果'), '第一个任务的草稿')
    await user.selectOptions(screen.getByRole('combobox', { name: '选择任务' }), 'task-2')
    expect(screen.getByLabelText('答案、代码或学习成果')).toHaveValue('')
    await user.type(screen.getByLabelText('答案、代码或学习成果'), '第二个任务的草稿')
    await user.selectOptions(screen.getByRole('combobox', { name: '选择任务' }), 'task-1')
    expect(screen.getByLabelText('答案、代码或学习成果')).toHaveValue('第一个任务的草稿')
  })

  it('loads every message page using the last received sequence', async () => {
    vi.mocked(api.messages).mockResolvedValueOnce({ items: [{ id: 'm1', seq: 1, role: 'assistant', type: 'proactive', content: '第一条建议', created_at: '2026-10-20T01:00:00Z' }], last_seq: 2, read_seq: 0, has_more: true }).mockResolvedValueOnce({ items: [{ id: 'm2', seq: 2, role: 'assistant', type: 'result', content: '第二条反馈', created_at: '2026-10-20T01:01:00Z' }], last_seq: 2, read_seq: 0, has_more: false })
    mount()
    expect(await screen.findByText('第一条建议')).toBeInTheDocument()
    expect(screen.getByText('第二条反馈')).toBeInTheDocument()
    expect(api.messages).toHaveBeenNthCalledWith(2, 'conversation-1', 1)
  })
  it('retains a failed attachment and blocks submission until retry or removal', async () => {
    const upload = vi.spyOn(api, 'upload').mockRejectedValueOnce(new ApiError(0, '上传连接中断')).mockResolvedValue({ id: 'attachment-1', filename: 'notes.txt' })
    const user = userEvent.setup()
    mount()
    const file = new File(['my work'], 'notes.txt', { type: 'text/plain' })
    await user.upload(screen.getByLabelText('添加附件'), file)
    expect(await screen.findByText('notes.txt 尚未上传成功')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '保存并请求评估' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '重试上传' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '保存并请求评估' })).toBeEnabled())
    expect(upload.mock.calls[0][0]).toBe(file)
    expect(upload.mock.calls[0][1]).toBe(upload.mock.calls[1][1])
    expect(screen.getByText('notes.txt')).toBeInTheDocument()
  })

})
