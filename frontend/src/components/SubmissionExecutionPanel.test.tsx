import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../api/client'
import type { SandboxExecution, Submission } from '../api/types'
import { bootstrap, goal, task } from '../test/fixtures'
import { SubmissionExecutionPanel } from './SubmissionExecutionPanel'

const refresh = vi.fn().mockResolvedValue(undefined)
vi.mock('../state', () => ({ useApp: () => ({ data: bootstrap, revision: 1, refresh }) }))
const submission: Submission = { id: 'submission-1', task_id: 'task-1', text: 'print("saved")', created_at: '2026-10-29T02:00:00Z' }
const current = { ...task, active_submission_id: submission.id }
const execution: SandboxExecution = {
  id: 'exec-1', operation_id: 'op-1', goal_id: goal.id, task_id: task.id, submission_id: submission.id,
  request_id: 'request-1', language: 'python', status: 'accepted', phase: 'queued', outcome: 'unknown',
  error_code: null, error_message: null, stdout: '', stderr: '', cleanup_done: false, delivery_done: false,
  created_at: '2026-10-29T02:00:00Z', updated_at: '2026-10-29T02:00:00Z', artifacts: [],
}

beforeEach(() => { refresh.mockClear() })

describe('saved submission execution controls', () => {
  it('executes only after an explicit click and reuses the idempotency key on network retry', async () => {
    const run = vi.spyOn(api, 'runSubmission').mockRejectedValueOnce(new ApiError(0, '运行连接中断')).mockResolvedValue(execution)
    const user = userEvent.setup()
    render(<SubmissionExecutionPanel submission={submission} goal={goal} task={current} executions={[]} />)
    expect(run).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '运行代码' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('运行连接中断')
    await user.click(screen.getByRole('button', { name: '运行代码' }))
    expect(await screen.findByText('等待运行')).toBeVisible()
    expect(run.mock.calls[0][0]).toBe(submission.id)
    expect(run.mock.calls[0][1]).toEqual({ expected_version: current.state_version, expected_goal_revision: goal.control_revision, language: 'python', attachment_id: null })
    expect(run.mock.calls[0][2]).toBe(run.mock.calls[1][2])
    expect(screen.getByRole('button', { name: '已有代码正在运行' })).toBeDisabled()
  })

  it('selects only saved Python attachments and preserves the selected file in the request', async () => {
    const run = vi.spyOn(api, 'runSubmission').mockResolvedValue({ ...execution, attachment_id: 'py-1' })
    const user = userEvent.setup()
    render(<SubmissionExecutionPanel submission={{ ...submission, text: '', attachments: [{ id: 'py-1', filename: 'main.py' }, { id: 'note-1', filename: 'notes.txt' }] }} goal={goal} task={current} executions={[]} />)
    expect(screen.getByRole('combobox', { name: '选择已保存的Python代码' })).toHaveValue('py-1')
    expect(screen.queryByRole('option', { name: 'notes.txt' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '运行代码' }))
    await waitFor(() => expect(run).toHaveBeenCalledWith(submission.id, expect.objectContaining({ attachment_id: 'py-1' }), expect.any(String)))
    expect(await screen.findByText(/运行内容：main.py/)).toBeVisible()
  })

  it.each([
    { name: 'paused goal', goal: { ...goal, status: 'paused' as const }, task: current },
    { name: 'replaced plan', goal: { ...goal, active_plan_id: 'new-plan' }, task: current },
    { name: 'replaced submission', goal, task: { ...current, active_submission_id: 'new-submission' } },
    { name: 'cancelled task', goal, task: { ...current, status: 'cancelled' as const } },
  ])('does not expose new execution for $name', ({ goal, task }) => {
    const run = vi.spyOn(api, 'runSubmission')
    render(<SubmissionExecutionPanel submission={submission} goal={goal} task={task} executions={[]} />)
    expect(screen.queryByRole('button', { name: '运行代码' })).not.toBeInTheDocument()
    expect(run).not.toHaveBeenCalled()
  })

  it('restores persisted execution output on remount and cancels through the saved execution id', async () => {
    const cancel = vi.spyOn(api, 'cancelSandboxExecution').mockResolvedValue({ ...execution, status: 'cancelling', updated_at: '2026-10-29T02:01:00Z' })
    const user = userEvent.setup()
    const view = render(<SubmissionExecutionPanel submission={submission} goal={goal} task={current} executions={[{ ...execution, status: 'running', stdout: 'saved output' }]} />)
    expect(screen.getByText('saved output')).toBeVisible()
    await user.click(screen.getByRole('button', { name: '取消运行' }))
    await waitFor(() => expect(cancel).toHaveBeenCalledWith(execution.id, expect.any(String)))
    expect(await screen.findByRole('button', { name: '正在取消…' })).toBeDisabled()
    view.unmount()
    render(<SubmissionExecutionPanel submission={submission} goal={goal} task={current} executions={[{ ...execution, status: 'cancelled', stdout: 'saved output', cleanup_done: true, delivery_done: true }]} />)
    expect(screen.getByText('saved output')).toBeVisible()
    expect(screen.getByText('已取消')).toBeVisible()
    expect(screen.getByText('隔离环境已清理')).toBeVisible()
  })
})
