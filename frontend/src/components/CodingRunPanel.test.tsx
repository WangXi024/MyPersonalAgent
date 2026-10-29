import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../api/client'
import type { Submission } from '../api/types'
import { bootstrap, goal, task } from '../test/fixtures'
import { codingRun } from '../test/coding-fixtures'
import { CodingRunPanel } from './CodingRunPanel'

const refresh = vi.fn().mockResolvedValue(undefined)
vi.mock('../state', () => ({ useApp: () => ({ data: bootstrap, revision: 1, refresh }) }))
const submission: Submission = { id: 'submission-1', task_id: task.id, text: 'print("saved code")', created_at: codingRun.created_at }
const current = { ...task, active_submission_id: submission.id }

describe('explicit coding assistant requests', () => {
  it('requires a specific instruction and an explicit click, without auto-submitting generated code', async () => {
    const create = vi.spyOn(api, 'createCodingRun').mockResolvedValue({ ...codingRun, status: 'accepted', workspace_created: false })
    const command = vi.spyOn(api, 'command')
    const upload = vi.spyOn(api, 'upload')
    const user = userEvent.setup()
    render(<CodingRunPanel submission={submission} goal={goal} task={current} runs={[]} />)
    expect(create).not.toHaveBeenCalled()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '编码助手' }))
    expect(screen.getByRole('button', { name: '启动编码助手' })).toBeDisabled()
    await user.type(screen.getByLabelText('希望编码助手做什么'), '解释错误并修改已保存的代码。')
    expect(create).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '启动编码助手' }))
    await waitFor(() => expect(create).toHaveBeenCalledWith(submission.id, { expected_version: task.state_version,
      expected_goal_revision: goal.control_revision, attachment_id: null, instruction: '解释错误并修改已保存的代码。' }, expect.any(String)))
    expect(await screen.findByText('等待编码助手')).toBeVisible()
    expect(command).not.toHaveBeenCalled()
    expect(upload).not.toHaveBeenCalled()
  })

  it('preserves the instruction and exact body/key when a network request needs retry', async () => {
    const create = vi.spyOn(api, 'createCodingRun').mockRejectedValueOnce(new ApiError(0, '编码连接中断')).mockResolvedValue(codingRun)
    const user = userEvent.setup()
    render(<CodingRunPanel submission={submission} goal={goal} task={current} runs={[]} />)
    await user.click(screen.getByRole('button', { name: '编码助手' }))
    const input = screen.getByLabelText('希望编码助手做什么')
    await user.type(input, '给函数增加输入校验')
    await user.click(screen.getByRole('button', { name: '启动编码助手' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('编码连接中断')
    expect(input).toHaveValue('给函数增加输入校验')
    await user.click(screen.getByRole('button', { name: '启动编码助手' }))
    expect(await screen.findByText('编码助手处理中')).toBeVisible()
    expect(create.mock.calls[0][1]).toEqual(create.mock.calls[1][1])
    expect(create.mock.calls[0][2]).toBe(create.mock.calls[1][2])
  })

  it('only offers saved Python attachments and sends their id rather than a file or draft', async () => {
    const create = vi.spyOn(api, 'createCodingRun').mockResolvedValue({ ...codingRun, attachment_id: 'py-1' })
    const user = userEvent.setup()
    render(<CodingRunPanel submission={{ ...submission, text: '', attachments: [{ id: 'py-1', filename: 'main.py' }, { id: 'note', filename: 'notes.txt' }] }} goal={goal} task={current} runs={[]} />)
    await user.click(screen.getByRole('button', { name: '编码助手' }))
    expect(screen.getByRole('combobox', { name: '编码助手代码来源' })).toHaveValue('py-1')
    expect(screen.queryByRole('option', { name: 'notes.txt' })).not.toBeInTheDocument()
    await user.type(screen.getByLabelText('希望编码助手做什么'), '解释这个文件')
    await user.click(screen.getByRole('button', { name: '启动编码助手' }))
    await waitFor(() => expect(create).toHaveBeenCalledWith(submission.id, expect.objectContaining({ attachment_id: 'py-1' }), expect.any(String)))
  })

  it.each([
    { name: 'paused goal', goal: { ...goal, status: 'paused' as const }, task: current },
    { name: 'old plan', goal: { ...goal, active_plan_id: 'other-plan' }, task: current },
    { name: 'old submission', goal, task: { ...current, active_submission_id: 'new-submission' } },
    { name: 'cancelled task', goal, task: { ...current, status: 'cancelled' as const } },
  ])('keeps historical results readable while preventing a new run for $name', ({ goal, task }) => {
    const create = vi.spyOn(api, 'createCodingRun')
    render(<CodingRunPanel submission={submission} goal={goal} task={task} runs={[{ ...codingRun, status: 'succeeded', cleanup_done: true }]} />)
    expect(screen.queryByRole('button', { name: '编码助手' })).not.toBeInTheDocument()
    expect(screen.getByText('编码助手已完成')).toBeVisible()
    expect(create).not.toHaveBeenCalled()
  })

  it('receives persistent snapshots, cancels by the real run id and waits for cleanup before another run', async () => {
    const cancel = vi.spyOn(api, 'cancelCodingRun').mockResolvedValue({ ...codingRun, status: 'cancelling', updated_at: '2026-10-29T04:02:00Z' })
    const user = userEvent.setup()
    const view = render(<CodingRunPanel submission={submission} goal={goal} task={current} runs={[codingRun]} />)
    await user.click(screen.getByRole('button', { name: '编码助手' }))
    expect(screen.getByRole('button', { name: '编码助手正在处理' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '停止编码助手' }))
    await waitFor(() => expect(cancel).toHaveBeenCalledWith(codingRun.id, expect.any(String)))
    expect(await screen.findByRole('button', { name: '正在停止…' })).toBeDisabled()
    view.rerender(<CodingRunPanel submission={submission} goal={goal} task={current} runs={[{ ...codingRun, status: 'succeeded', stage: 'finished', summary: '生成文件已保存。', updated_at: '2026-10-29T04:03:00Z' }]} />)
    expect(screen.getByText('生成文件已保存。')).toBeVisible()
    expect(screen.getByRole('button', { name: '等待工作区清理' })).toBeDisabled()
    view.unmount()
    render(<CodingRunPanel submission={submission} goal={goal} task={current} runs={[{ ...codingRun, status: 'succeeded', stage: 'finished', summary: '生成文件已保存。', cleanup_done: true }]} />)
    expect(screen.getByText('生成文件已保存。')).toBeVisible()
    expect(screen.getByText('工作区已销毁')).toBeVisible()
  })

  it('associates a cancellation failure with its own run instead of unrelated historical records', async () => {
    vi.spyOn(api, 'cancelCodingRun').mockRejectedValue(new ApiError(503, '暂时未能停止此任务'))
    const user = userEvent.setup()
    render(<CodingRunPanel submission={submission} goal={goal} task={current} runs={[codingRun,
      { ...codingRun, id: 'previous-run', status: 'succeeded', cleanup_done: true, created_at: '2026-10-28T01:00:00Z' },
    ]} />)
    await user.click(screen.getByRole('button', { name: '停止编码助手' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('暂时未能停止此任务')
    expect(within(screen.getByRole('region', { name: `编码助手记录：${codingRun.id}` })).getByRole('alert')).toBeVisible()
    expect(within(screen.getByRole('region', { name: '编码助手记录：previous-run' })).queryByRole('alert')).not.toBeInTheDocument()
  })

  it('keeps old results readable but rejects a saved submission from an outdated requirements version', () => {
    const create = vi.spyOn(api, 'createCodingRun')
    render(<CodingRunPanel submission={{ ...submission, requirements_version: current.requirements_version - 1 }} goal={goal} task={current} runs={[{ ...codingRun, status: 'succeeded', cleanup_done: true }]} />)
    expect(screen.getByText('编码助手已完成')).toBeVisible()
    expect(screen.queryByRole('button', { name: '编码助手' })).not.toBeInTheDocument()
    expect(create).not.toHaveBeenCalled()
  })

  it('blocks a new run while cleanup is unconfirmed after a lost workspace creation receipt', async () => {
    const create = vi.spyOn(api, 'createCodingRun')
    const user = userEvent.setup()
    const view = render(<CodingRunPanel submission={submission} goal={goal} task={current} runs={[{
      ...codingRun, status: 'failed', workspace_created: false, cleanup_done: false,
    }]} />)
    await user.click(screen.getByRole('button', { name: '编码助手' }))
    await user.type(screen.getByLabelText('希望编码助手做什么'), '检查并解释代码')
    expect(screen.getByRole('button', { name: '等待工作区清理' })).toBeDisabled()
    expect(create).not.toHaveBeenCalled()
    view.rerender(<CodingRunPanel submission={submission} goal={goal} task={current} runs={[{
      ...codingRun, status: 'failed', workspace_created: false, cleanup_done: true,
    }]} />)
    await user.type(screen.getByLabelText('希望编码助手做什么'), '检查并解释代码')
    expect(screen.getByRole('button', { name: '启动编码助手' })).toBeEnabled()
    expect(create).not.toHaveBeenCalled()
  })
})
