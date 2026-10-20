import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { Task } from '../api/types'
import { detail as baseDetail, task } from '../test/fixtures'
import { GoalDetailPage } from './GoalDetail'
let detail = structuredClone(baseDetail)
vi.mock('../state', () => ({ useApp: () => ({ refresh: vi.fn().mockResolvedValue(undefined), revision: 1 }) }))
vi.mock('../hooks', async importOriginal => { const original = await importOriginal<typeof import('../hooks')>(); return { ...original, useGoal: () => ({ detail, error: null }) } })
beforeEach(() => { detail = structuredClone(baseDetail) })
function mount() { return render(<MemoryRouter initialEntries={['/goals/goal-1']}><Routes><Route path="/goals/:goalId" element={<GoalDetailPage />} /></Routes></MemoryRouter>) }
function makeTask(id: string, overrides: Partial<Task> = {}): Task { return { ...task, id, title: id, ...overrides } }
function milestoneBranch(title: string) {
  const branch = screen.getByRole('heading', { name: title, level: 4 }).closest('details')
  expect(branch).not.toBeNull()
  return branch!
}
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
  it('groups current tasks under ordered milestones and keeps completion separate from evidence', async () => {
    detail.milestones = [
      { id: 'practice', plan_id: 'plan-1', title: '项目实践', criteria: ['独立交付项目'], status: 'unverified', position: 2 },
      { id: 'old-stage', plan_id: 'old-plan', title: '旧计划阶段', criteria: ['旧标准'], status: 'evidence_available', position: 0 },
      { id: 'basics', plan_id: 'plan-1', title: '基础语法', criteria: ['解释变量与分支'], status: 'unverified', position: 1 },
      { id: 'empty', plan_id: 'plan-1', title: '成果整理', criteria: ['保存成果与依据'], status: 'evidence_available', position: 3 },
    ]
    detail.tasks = [
      makeTask('项目练习', { milestone_id: 'practice', status: 'in_progress' }),
      makeTask('已跳过的分支练习', { milestone_id: 'basics', status: 'skipped' }),
      makeTask('已完成的变量练习', { milestone_id: 'basics', status: 'completed' }),
      makeTask('待开始的函数练习', { milestone_id: 'basics', status: 'ready' }),
    ]
    const user = userEvent.setup()
    mount()
    const basics = milestoneBranch('基础语法')
    const practice = milestoneBranch('项目实践')
    const empty = milestoneBranch('成果整理')
    expect(basics.compareDocumentPosition(practice) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(practice.compareDocumentPosition(empty) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.queryByText('旧计划阶段')).not.toBeInTheDocument()
    expect(screen.getByText('当前计划已完成 1 / 4 项任务；跳过不计为完成。')).toBeInTheDocument()
    expect(screen.getByText('有学习证据 1 / 3 个里程碑')).toBeInTheDocument()
    expect(within(basics).getByText('已完成 1 / 3')).toBeInTheDocument()
    expect(within(basics).getByText('待验证')).toBeInTheDocument()
    expect(within(basics).getByText('解释变量与分支')).toBeInTheDocument()
    expect(within(basics).getByText('已跳过')).toBeInTheDocument()
    expect(within(basics).queryByText('项目练习')).not.toBeInTheDocument()
    expect(within(practice).getByRole('link', { name: /项目练习/ })).toHaveAttribute('href', '/goals/goal-1/learn?task=项目练习')
    expect(within(empty).getByText('此里程碑暂无当前任务')).toBeInTheDocument()
    expect(within(empty).getByText('保存成果与依据')).toBeInTheDocument()
    expect(within(empty).getByText('已有学习证据')).toBeInTheDocument()
    expect(within(empty).getByText('已完成 0 / 0')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '确认目标已完成' })).not.toBeInTheDocument()
    const summary = basics.querySelector('summary')!
    expect(basics).toHaveAttribute('open')
    await user.click(summary)
    expect(basics).not.toHaveAttribute('open')
    await user.click(summary)
    expect(basics).toHaveAttribute('open')
    expect(within(basics).getByRole('link', { name: /已完成的变量练习/ })).toHaveAttribute('href', '/goals/goal-1/learn?task=已完成的变量练习')
  })
  it('keeps carried tasks and historical tasks outside current-plan progress', async () => {
    detail.tasks = [
      makeTask('当前完成任务', { status: 'completed' }),
      makeTask('当前跳过任务', { status: 'skipped' }),
      makeTask('当前取消任务', { status: 'cancelled' }),
      makeTask('旧计划学习中', { plan_id: 'old-plan', status: 'in_progress' }),
      makeTask('旧计划待评估', { plan_id: 'old-plan', status: 'awaiting_review' }),
      makeTask('旧计划待巩固', { plan_id: 'old-plan', status: 'needs_revision' }),
      makeTask('旧计划已完成', { plan_id: 'old-plan', status: 'completed' }),
      makeTask('旧计划待开始', { plan_id: 'old-plan', status: 'ready' }),
      makeTask('旧计划已跳过', { plan_id: 'old-plan', status: 'skipped' }),
    ]
    const user = userEvent.setup()
    mount()
    const current = milestoneBranch('未归入里程碑的任务')
    const carried = screen.getByRole('heading', { name: '从之前计划保留的任务' }).parentElement!
    const historySummary = screen.getByText('查看历史任务（4）')
    const history = historySummary.closest('details')!
    expect(screen.getByText('当前计划已完成 1 / 2 项任务；跳过不计为完成。')).toBeInTheDocument()
    expect(within(current).getByText('当前完成任务')).toBeInTheDocument()
    expect(within(current).getByText('当前跳过任务')).toBeInTheDocument()
    for (const title of ['旧计划学习中', '旧计划待评估', '旧计划待巩固']) {
      expect(within(carried).getByRole('link', { name: new RegExp(title) })).toBeInTheDocument()
      expect(within(current).queryByText(title)).not.toBeInTheDocument()
      expect(within(history).queryByText(title)).not.toBeInTheDocument()
    }
    expect(history).not.toHaveAttribute('open')
    await user.click(historySummary)
    expect(history).toHaveAttribute('open')
    for (const title of ['当前取消任务', '旧计划已完成', '旧计划待开始', '旧计划已跳过']) {
      expect(within(history).getByRole('link', { name: new RegExp(title) })).toBeInTheDocument()
      expect(within(current).queryByText(title)).not.toBeInTheDocument()
      expect(within(carried).queryByText(title)).not.toBeInTheDocument()
    }
  })
  it('shows tasks with null, missing, or old-plan milestones in the unassigned branch', () => {
    detail.milestones = [
      { id: 'current-stage', plan_id: 'plan-1', title: '当前里程碑', criteria: [], status: 'unverified', position: 0 },
      { id: 'old-stage', plan_id: 'old-plan', title: '旧里程碑', criteria: [], status: 'unverified', position: 0 },
    ]
    detail.tasks = [
      makeTask('未指定里程碑', { milestone_id: null }),
      makeTask('里程碑已失联', { milestone_id: 'missing-stage' }),
      makeTask('关联旧计划里程碑', { milestone_id: 'old-stage' }),
      makeTask('正常关联任务', { milestone_id: 'current-stage' }),
    ]
    mount()
    const unassigned = milestoneBranch('未归入里程碑的任务')
    for (const title of ['未指定里程碑', '里程碑已失联', '关联旧计划里程碑']) {
      expect(within(unassigned).getByRole('link', { name: new RegExp(title) })).toBeInTheDocument()
    }
    expect(within(unassigned).getByText('已完成 0 / 3')).toBeInTheDocument()
    expect(within(unassigned).queryByText('正常关联任务')).not.toBeInTheDocument()
    expect(within(milestoneBranch('当前里程碑')).getByText('正常关联任务')).toBeInTheDocument()
    expect(screen.queryByText('旧里程碑')).not.toBeInTheDocument()
  })
  it('keeps proposed milestones and tasks in the confirmation card until the plan is accepted', () => {
    detail.goal = { ...detail.goal, status: 'draft', active_plan_id: null }
    detail.tasks = []
    detail.plans = [{
      id: 'proposal-1', goal_id: detail.goal.id, version: 1, status: 'proposed', summary: '候选学习安排', created_at: '2026-10-20T01:00:00Z',
      proposal: {
        summary: '候选学习安排', message: '确认后开始推进',
        milestones: [{ title: '候选里程碑', criteria: ['候选完成标准'] }],
        tasks: [{ title: '候选练习', instructions: '等待确认后练习', criteria: ['候选练习标准'], skills: [], estimated_minutes: 15, scheduled_date: '2026-10-20', milestone_index: 0 }],
      },
    }]
    mount()
    const tree = screen.getByRole('heading', { name: '任务树', level: 2 }).closest('section')!
    expect(screen.getByText('候选里程碑')).toBeInTheDocument()
    expect(screen.getByText('候选练习')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确认这个计划' })).toBeEnabled()
    expect(within(tree).getByText('任务树将在计划确认后出现')).toBeInTheDocument()
    expect(within(tree).queryByText('候选里程碑')).not.toBeInTheDocument()
    expect(within(tree).queryByText('候选练习')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /候选练习/ })).not.toBeInTheDocument()
  })
  it('preserves a collapsed milestone while refreshed task status and counts update', async () => {
    detail.milestones = [{ id: 'milestone-1', plan_id: 'plan-1', title: '基础练习', criteria: ['完成一次练习'], status: 'unverified', position: 0 }]
    detail.tasks = [makeTask('可刷新的任务', { status: 'in_progress' })]
    const user = userEvent.setup()
    const view = mount()
    await user.click(milestoneBranch('基础练习').querySelector('summary')!)
    expect(milestoneBranch('基础练习')).not.toHaveAttribute('open')
    detail = {
      ...detail,
      milestones: [{ ...detail.milestones[0], status: 'evidence_available' }],
      tasks: [{ ...detail.tasks[0], status: 'completed', state_version: 2 }],
    }
    view.rerender(<MemoryRouter initialEntries={['/goals/goal-1']}><Routes><Route path="/goals/:goalId" element={<GoalDetailPage />} /></Routes></MemoryRouter>)
    const branch = milestoneBranch('基础练习')
    expect(branch).not.toHaveAttribute('open')
    expect(within(branch).getByText('已完成 1 / 1')).toBeInTheDocument()
    expect(within(branch).getByText('已有学习证据')).toBeInTheDocument()
    expect(screen.getByText('当前计划已完成 1 / 1 项任务；跳过不计为完成。')).toBeInTheDocument()
    expect(screen.getByText('有学习证据 1 / 1 个里程碑')).toBeInTheDocument()
    await user.click(branch.querySelector('summary')!)
    expect(branch).toHaveAttribute('open')
    expect(within(branch).getByRole('link', { name: /可刷新的任务/ })).toHaveTextContent('已完成')
  })
})
