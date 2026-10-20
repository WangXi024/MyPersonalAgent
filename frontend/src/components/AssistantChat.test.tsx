import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { bootstrap } from '../test/fixtures'
import { Today } from '../pages/Today'
import { AssistantChat } from './AssistantChat'
let data = structuredClone(bootstrap)
const refresh = vi.fn().mockResolvedValue(undefined)
const globalConversation = { id: 'assistant-conversation', goal_id: null, last_seq: 0, read_seq: 0, unread_count: 0 }
vi.mock('../state', () => ({ useApp: () => ({ data, revision: 1, refresh }) }))
beforeEach(() => {
  data = { ...structuredClone(bootstrap), goals: [], tasks: [], conversations: [] }
  vi.spyOn(api, 'assistantConversation').mockResolvedValue(globalConversation)
  vi.spyOn(api, 'messages').mockResolvedValue({ items: [], last_seq: 0, read_seq: 0, has_more: false })
  vi.stubGlobal('IntersectionObserver', class { observe() {} disconnect() {} })
})
describe('direct Agent conversation', () => {
  it('lets a user with no goals send directly from the Today page', async () => {
    const command = vi.spyOn(api, 'command').mockResolvedValue({ operation_id: 'global-operation', status: 'accepted' })
    const createGoal = vi.spyOn(api, 'createGoal')
    const user = userEvent.setup()
    render(<MemoryRouter><Today /></MemoryRouter>)
    const input = screen.getByLabelText('给学习伙伴发消息')
    expect(input).toBeVisible()
    await user.type(input, '我想学点新东西，但还没有目标。')
    await user.click(screen.getByRole('button', { name: '发送消息' }))
    await waitFor(() => expect(command).toHaveBeenCalledOnce())
    expect(command.mock.calls[0][0]).toBe('/conversations/assistant-conversation/messages')
    expect(command.mock.calls[0][1]).toEqual({ content: '我想学点新东西，但还没有目标。' })
    expect(createGoal).not.toHaveBeenCalled()
    expect(input).toHaveValue('')
  })
  it('reuses the existing global conversation and exposes failed-operation retry', async () => {
    data.conversations = [globalConversation]
    data.operations = [{ id: 'global-failed', goal_id: null, kind: 'tutor', status: 'failed', error_message: '回复暂未生成', created_at: '2026-10-20T01:00:00Z' }]
    const command = vi.spyOn(api, 'command').mockResolvedValue({ operation_id: 'global-retry', status: 'accepted' })
    const user = userEvent.setup()
    render(<MemoryRouter><AssistantChat /></MemoryRouter>)
    expect(api.assistantConversation).not.toHaveBeenCalled()
    expect(screen.getByText('回复暂未生成')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重试此操作' }))
    expect(command).toHaveBeenCalledWith('/operations/global-failed/retry', {}, expect.any(String))
  })
})
