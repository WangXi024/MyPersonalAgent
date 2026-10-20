import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { GoalDraft } from '../api/types'
import { bootstrap } from '../test/fixtures'
import { Today } from '../pages/Today'
import { AssistantChat } from './AssistantChat'
let data = structuredClone(bootstrap)
let revision = 1
const refresh = vi.fn().mockResolvedValue(undefined)
const globalConversation = { id: 'assistant-conversation', goal_id: null, last_seq: 0, read_seq: 0, unread_count: 0 }
vi.mock('../state', () => ({ useApp: () => ({ data, revision, refresh }) }))
beforeEach(() => {
  revision = 1
  data = { ...structuredClone(bootstrap), goals: [], tasks: [], conversations: [] }
  vi.spyOn(api, 'assistantConversation').mockResolvedValue(globalConversation)
  vi.spyOn(api, 'messages').mockResolvedValue({ items: [], last_seq: 0, read_seq: 0, has_more: false })
  vi.spyOn(api, 'goalDrafts').mockResolvedValue({ items: [] })
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
  it('refreshes draft acceptance on SSE revision even when the associated message is already cached', async () => {
    data.conversations = [globalConversation]
    const draft: GoalDraft = { operation_id: 'draft-operation', message_id: 'draft-message', status: 'pending', draft: { title: '学习绘画', background: '零基础', outcome: '完成一幅作品', criteria: ['独立完成'], deadline: null }, goal_id: null, plan_operation_id: null }
    vi.mocked(api.goalDrafts).mockResolvedValue({ items: [draft] })
    vi.mocked(api.messages).mockResolvedValueOnce({ items: [{ id: 'draft-message', seq: 1, role: 'assistant', type: 'assistant_reply', content: '这是我们整理好的目标草稿。', created_at: '2026-10-20T01:00:00Z' }], last_seq: 1, read_seq: 0, has_more: false })
    vi.spyOn(api, 'operation').mockResolvedValue({ id: 'planning', kind: 'plan', goal_id: 'drawing', status: 'accepted', created_at: '2026-10-20T01:00:00Z' })
    const accept = vi.spyOn(api, 'acceptGoalDraft')
    const view = render(<MemoryRouter><AssistantChat /></MemoryRouter>)
    expect(await screen.findByRole('button', { name: '确认创建目标' })).toBeInTheDocument()
    expect(accept).not.toHaveBeenCalled()
    vi.mocked(api.goalDrafts).mockResolvedValue({ items: [{ ...draft, status: 'accepted', goal_id: 'drawing', plan_operation_id: 'planning' }] })
    revision += 1
    view.rerender(<MemoryRouter><AssistantChat /></MemoryRouter>)
    expect(await screen.findByText('目标已创建')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '确认创建目标' })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: '查看目标与计划' })).toHaveAttribute('href', '/goals/drawing')
    expect(api.messages).toHaveBeenLastCalledWith('assistant-conversation', 1)
    expect(api.goalDrafts).toHaveBeenCalledTimes(2)
  })
})
