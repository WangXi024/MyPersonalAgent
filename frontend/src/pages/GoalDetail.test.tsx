import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { detail as baseDetail, task } from '../test/fixtures'
import { GoalDetailPage } from './GoalDetail'
let detail = structuredClone(baseDetail)
vi.mock('../state', () => ({ useApp: () => ({ refresh: vi.fn().mockResolvedValue(undefined), revision: 1 }) }))
vi.mock('../hooks', async importOriginal => { const original = await importOriginal<typeof import('../hooks')>(); return { ...original, useGoal: () => ({ detail, error: null }) } })
beforeEach(() => { detail = structuredClone(baseDetail) })
function mount() { render(<MemoryRouter initialEntries={['/goals/goal-1']}><Routes><Route path="/goals/:goalId" element={<GoalDetailPage />} /></Routes></MemoryRouter>) }
describe('goal detail contract', () => {
  it('counts evidence_available only in the active plan and enables completion', () => {
    detail.milestones = [
      { id: 'm1', plan_id: 'plan-1', title: '当前阶段', criteria: ['完成练习'], status: 'evidence_available', position: 0 },
      { id: 'm0', plan_id: 'old-plan', title: '旧计划阶段', criteria: [], status: 'unverified', position: 0 },
    ]
    mount()
    expect(screen.getByText('有学习证据 1 / 1 个里程碑')).toBeInTheDocument()
    expect(screen.queryByText('旧计划阶段')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确认目标已完成' })).toBeEnabled()
    expect(screen.queryByText(/已验证.*里程碑/)).not.toBeInTheDocument()
  })
  it('marks superseded submission feedback as history and shows AI evidence limitations', () => {
    detail.tasks = [{ ...task, active_submission_id: 'new-submission' }]
    detail.evaluations = [{ id: 'old-evaluation', submission_id: 'old-submission', passed: true, feedback: '旧答案符合要求', evidence_type: 'ai_review', valid: true, details: {}, created_at: '2026-10-20T01:00:00Z' }]
    mount()
    expect(screen.getByText('历史评估')).toBeInTheDocument()
    expect(screen.getByText('这是 AI 按任务标准进行的审阅，未执行用户代码。')).toBeInTheDocument()
  })
  it('edits the goal with explicit replanning confirmation and version protection', async () => {
    const update = vi.spyOn(api, 'updateGoal').mockResolvedValue({ operation_id: 'edit-op', status: 'accepted', resource_id: 'goal-1' })
    const user = userEvent.setup()
    mount()
    await user.click(screen.getByRole('button', { name: '修改目标' }))
    expect(screen.getByText('修改会生成新计划，需要重新确认。已有提交和学习证据会保留。')).toBeInTheDocument()
    await user.clear(screen.getByLabelText('目标名称'))
    await user.type(screen.getByLabelText('目标名称'), '学习 Python 数据处理')
    await user.click(screen.getByRole('button', { name: '保存并重新规划' }))
    await waitFor(() => expect(update).toHaveBeenCalledOnce())
    expect(update.mock.calls[0]).toEqual(['goal-1', expect.objectContaining({ title: '学习 Python 数据处理', expected_version: 1, criteria: ['独立完成'], deadline: null }), expect.any(String)])
  })
})
