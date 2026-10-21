import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../api/client'
import type { GoalDraft } from '../api/types'
import { bootstrap } from '../test/fixtures'
import { GoalDraftCard } from './GoalDraftCard'

const refresh = vi.fn().mockResolvedValue(undefined)
let revision = 1
vi.mock('../state', () => ({ useApp: () => ({ data: bootstrap, revision, refresh }) }))
const proposal: GoalDraft = { operation_id: 'draft-op', message_id: 'draft-message', status: 'pending', draft: { title: '学习 Python', background: '零基础', outcome: '完成一个记账工具', criteria: ['能记录和汇总支出'], deadline: null }, goal_id: null, plan_operation_id: null }
const accepted = { operation_id: 'planning-op', status: 'accepted', resource_id: 'created-goal' }
const planning = { id: 'planning-op', goal_id: 'created-goal', kind: 'plan', status: 'running' as const, stage: 'reasoning', created_at: '2026-10-20T01:00:00Z' }
function mount(value = proposal) { return render(<MemoryRouter><GoalDraftCard proposal={value} /></MemoryRouter>) }
beforeEach(() => {
  revision = 1
  vi.spyOn(api, 'acceptGoalDraft').mockResolvedValue(accepted)
  vi.spyOn(api, 'operation').mockResolvedValue(planning)
})

describe('goal draft confirmation', () => {
  it('keeps an editable proposal inert until the user explicitly confirms all fields', async () => {
    const create = vi.spyOn(api, 'createGoal')
    const settings = vi.spyOn(api, 'updateSettings')
    const user = userEvent.setup()
    mount()
    expect(screen.getByText('确认后创建并准备计划，计划仍需确认。')).toBeInTheDocument()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '修改' }))
    for (const [label, value] of [['学习目标', '编写记账程序'], ['现在的基础', '会变量和循环'], ['预期成果', '独立完成命令行工具'], [/完成标准/, '能保存记录\n能解释实现']] as const) {
      const input = screen.getByLabelText(label)
      await user.clear(input)
      await user.type(input, value)
    }
    await user.type(screen.getByLabelText(/希望完成的日期/), '2026-11-30')
    expect(api.acceptGoalDraft).not.toHaveBeenCalled()
    expect(create).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '确认创建目标' }))
    expect(api.acceptGoalDraft).toHaveBeenCalledWith('draft-op', { title: '编写记账程序', background: '会变量和循环', outcome: '独立完成命令行工具', criteria: ['能保存记录', '能解释实现'], deadline: '2026-11-30' }, expect.any(String))
    expect(await screen.findByText('目标已创建')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '查看目标与计划' })).toHaveAttribute('href', '/goals/created-goal')
    expect(settings).not.toHaveBeenCalled()
    expect(create).not.toHaveBeenCalled()
  })

  it('retries an uncertain network failure with the same payload and idempotency key', async () => {
    vi.mocked(api.acceptGoalDraft).mockRejectedValueOnce(new ApiError(0, '确认请求连接中断')).mockResolvedValue(accepted)
    const user = userEvent.setup()
    const view = mount()
    await user.click(screen.getByRole('button', { name: '确认创建目标' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('确认请求连接中断')
    revision += 1
    view.rerender(<MemoryRouter><GoalDraftCard proposal={{ ...proposal }} /></MemoryRouter>)
    await user.click(screen.getByRole('button', { name: '确认创建目标' }))
    await screen.findByText('目标已创建')
    expect(vi.mocked(api.acceptGoalDraft).mock.calls[1]).toEqual(vi.mocked(api.acceptGoalDraft).mock.calls[0])
    expect(vi.mocked(api.acceptGoalDraft).mock.calls[1][1].deadline).toBeNull()
  })

  it('keeps accepted cards and reloads planning progress even when it is outside the bootstrap operation window', async () => {
    const view = mount({ ...proposal, status: 'accepted', goal_id: 'created-goal', plan_operation_id: 'planning-op' })
    expect(screen.queryByRole('button', { name: '确认创建目标' })).not.toBeInTheDocument()
    expect(await screen.findByText('正在生成与当前目标有关的建议。')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '查看目标与计划' })).toHaveAttribute('href', '/goals/created-goal')
    vi.mocked(api.operation).mockResolvedValue({ ...planning, status: 'succeeded', stage: 'saved' })
    revision += 1
    view.rerender(<MemoryRouter><GoalDraftCard proposal={{ ...proposal, status: 'accepted', goal_id: 'created-goal', plan_operation_id: 'planning-op' }} /></MemoryRouter>)
    expect(await screen.findByText('结果已保存。')).toBeInTheDocument()
    expect(api.acceptGoalDraft).not.toHaveBeenCalled()
  })

  it('disables an old card when the server supersedes it while it is being edited', async () => {
    const user = userEvent.setup()
    const view = mount()
    await user.click(screen.getByRole('button', { name: '修改' }))
    view.rerender(<MemoryRouter><GoalDraftCard proposal={{ ...proposal, status: 'superseded' }} /></MemoryRouter>)
    expect(screen.getByText('这份草稿已失效，请查看对话中的最新草稿。')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '确认创建目标' })).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(api.acceptGoalDraft).not.toHaveBeenCalled()
  })

  it('rejects blank or overlong criteria before attempting creation', async () => {
    const user = userEvent.setup()
    mount({ ...proposal, draft: { ...proposal.draft, criteria: ['a'.repeat(501)] } })
    await user.click(screen.getByRole('button', { name: '确认创建目标' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('每项不超过 500 字'))
    expect(api.acceptGoalDraft).not.toHaveBeenCalled()
  })

  it('lets the accepted goal card retry failed planning directly', async () => {
    vi.mocked(api.operation).mockResolvedValue({ ...planning, status: 'failed', stage: 'failed', error_message: '模型响应超时' })
    const command = vi.spyOn(api, 'command').mockResolvedValue({ operation_id: 'new-plan', status: 'accepted' })
    const user = userEvent.setup()
    mount({ ...proposal, status: 'accepted', goal_id: 'created-goal', plan_operation_id: 'planning-op' })
    await user.click(await screen.findByRole('button', { name: '重试生成计划' }))
    expect(command).toHaveBeenCalledWith('/operations/planning-op/retry', {}, expect.any(String))
    await waitFor(() => expect(api.operation).toHaveBeenCalledWith('new-plan'))
    expect(api.acceptGoalDraft).not.toHaveBeenCalled()
  })

  it('shows the current goal facts after an accepted draft is changed', async () => {
    const goal = bootstrap.goals[0]
    const original = { ...proposal.draft, title: '旧标题', background: '旧基础' }
    mount({ ...proposal, draft: original, status: 'accepted', goal_id: goal.id, plan_operation_id: 'planning-op' })
    expect(screen.getByRole('heading', { name: goal.title })).toBeInTheDocument()
    expect(screen.getByText(goal.background)).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: original.title })).not.toBeInTheDocument()
  })

  it('distinguishes an accepted historical draft from its now cancelled goal', async () => {
    vi.mocked(api.operation).mockResolvedValue({ ...planning, status: 'failed', stage: 'failed' })
    mount({ ...proposal, status: 'accepted', goal_id: 'cancelled-goal', goal_status: 'cancelled', plan_operation_id: 'planning-op' })
    expect(screen.getByText('已取消的目标')).toBeInTheDocument()
    expect(screen.getByText('这个目标已取消，历史记录保留。')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '查看目标历史' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '确认创建目标' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '重试生成计划' })).not.toBeInTheDocument()
  })
})
