import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../api/client'
import { goal, task as baseTask } from '../test/fixtures'
import { testContract, verificationRun } from '../test/verification-fixtures'
import { VerificationPanel } from './VerificationPanel'
import type { Goal, Task } from '../api/types'
import type { VerificationData } from '../api/verification'
const refresh = vi.fn().mockResolvedValue(undefined)
vi.mock('../state', () => ({ useApp: () => ({ refresh }) }))
const submission = { id: 'submission-1', task_id: 'task-1', text: 'print(sum(map(int, input().split())))', requirements_version: 1, created_at: '2026-10-29T00:00:00Z' }
const task = { ...baseTask, active_submission_id: submission.id }
const empty = { contracts: [], runs: [] }
beforeEach(() => {
  vi.spyOn(api, 'confirmTestContract').mockResolvedValue(testContract)
  vi.spyOn(api, 'createVerification').mockResolvedValue(verificationRun)
  vi.spyOn(api, 'cancelVerification').mockResolvedValue({ ...verificationRun, status: 'cancelling' })
})
function mount(data: VerificationData = empty, currentTask: Task = task, currentGoal: Goal = goal) { return render(<VerificationPanel submission={submission} task={currentTask} goal={currentGoal} data={data} ready />) }
function fill() {
  fireEvent.change(screen.getByLabelText('标准输入 stdin'), { target: { value: '2 3\n' } })
  fireEvent.change(screen.getByLabelText('预期标准输出 stdout'), { target: { value: '5\n' } })
}
describe('human-confirmed Python verification', () => {
  it('runs only the latest confirmed contract and keeps earlier versions read-only', async () => {
    const user = userEvent.setup()
    const latest = { ...testContract, id: 'contract-2', version: 2, contract_hash: 'd'.repeat(64) }
    mount({ contracts: [testContract, latest], runs: [] })
    expect(screen.getByRole('combobox', { name: '选择已确认测试规则' })).toHaveValue(latest.id)
    expect(screen.queryByRole('option', { name: '规则 v1 · 要求 v1' })).not.toBeInTheDocument()
    expect(screen.getByText('历史确认规则 v1 · 要求 v1')).toBeVisible()
    await user.click(screen.getByRole('button', { name: '按确认规则验证代码' }))
    expect(api.createVerification).toHaveBeenCalledWith(submission.id, expect.objectContaining({ contract_id: latest.id, expected_contract_hash: latest.contract_hash }), expect.any(String))
  })
  it('requires reviewed rule confirmation, then a separate click to execute saved code', async () => {
    const user = userEvent.setup()
    mount(); fill()
    expect(api.confirmTestContract).not.toHaveBeenCalled(); expect(api.createVerification).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: '确认测试规则' })).toBeDisabled()
    await user.click(screen.getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: '确认测试规则' }))
    expect(api.confirmTestContract).toHaveBeenCalledWith(task.id, expect.objectContaining({ confirm: true, requirements_version: 1, expected_version: 1, expected_goal_revision: 1, comparator: 'normalize_newlines', cases: [{ name: '样例 1', stdin: '2 3\n', expected_stdout: '5\n' }] }), expect.any(String))
    expect(api.createVerification).not.toHaveBeenCalled()
    await user.click(await screen.findByRole('button', { name: '按确认规则验证代码' }))
    expect(api.createVerification).toHaveBeenCalledWith(submission.id, { expected_version: 1, expected_goal_revision: 1, contract_id: testContract.id, expected_contract_hash: testContract.contract_hash, attachment_id: null }, expect.any(String))
    expect(await screen.findByRole('region', { name: `程序验证记录：${verificationRun.id}` })).toBeVisible()
  })
  it('invalidates local approval whenever a case or task version changes', async () => {
    const user = userEvent.setup(), view = mount(); fill()
    await user.click(screen.getByRole('checkbox'))
    expect(screen.getByRole('button', { name: '确认测试规则' })).toBeEnabled()
    fireEvent.change(screen.getByLabelText('预期标准输出 stdout'), { target: { value: '6\n' } })
    expect(screen.getByRole('checkbox')).not.toBeChecked()
    await user.click(screen.getByRole('checkbox'))
    view.rerender(<VerificationPanel submission={submission} task={{ ...task, state_version: 2 }} goal={goal} data={empty} ready />)
    expect(screen.getByRole('checkbox')).not.toBeChecked()
    expect(api.confirmTestContract).not.toHaveBeenCalled()
  })
  it('bounds a contract at six cases and keeps empty output as a legitimate explicit expectation', async () => {
    const user = userEvent.setup(); mount()
    for (let count = 1; count < 6; count++) await user.click(screen.getByRole('button', { name: '添加样例' }))
    expect(screen.getByRole('button', { name: '添加样例' })).toBeDisabled()
    expect(screen.getAllByLabelText('预期标准输出 stdout')).toHaveLength(6)
    await user.click(screen.getByRole('checkbox'))
    expect(screen.getByRole('button', { name: '确认测试规则' })).toBeEnabled()
  })
  it('retries the same source and contract with the original versioned payload and key after network uncertainty', async () => {
    const user = userEvent.setup()
    vi.mocked(api.createVerification).mockRejectedValueOnce(new ApiError(0, '网络中断')).mockResolvedValue(verificationRun)
    const data = { contracts: [testContract], runs: [] }, view = mount(data)
    await user.click(screen.getByRole('button', { name: '按确认规则验证代码' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('网络中断')
    view.rerender(<VerificationPanel submission={submission} task={{ ...task, state_version: 2 }} goal={goal} data={data} ready />)
    await user.click(screen.getByRole('button', { name: '按确认规则验证代码' }))
    await waitFor(() => expect(api.createVerification).toHaveBeenCalledTimes(2))
    expect(vi.mocked(api.createVerification).mock.calls[1]).toEqual(vi.mocked(api.createVerification).mock.calls[0])
    expect(vi.mocked(api.createVerification).mock.calls[1][1].expected_version).toBe(1)
  })
  it.each([{ ...task, active_submission_id: 'newer-submission' }, { ...task, requirements_version: 2 }, { ...task, plan_id: 'old-plan' }])('blocks stale source or requirements while retaining historical verification records', stale => {
    mount({ contracts: [testContract], runs: [verificationRun] }, stale)
    expect(screen.queryByRole('button', { name: '按确认规则验证代码' })).not.toBeInTheDocument()
    expect(screen.getByText('历史确认规则 v1 · 要求 v1')).toBeVisible()
    expect(screen.getByRole('region', { name: `程序验证记录：${verificationRun.id}` })).toBeVisible()
    expect(api.createVerification).not.toHaveBeenCalled()
  })
  it('keeps a slot closed until a prior terminal run has confirmed cleanup and permits explicit cleanup retry', async () => {
    const user = userEvent.setup()
    mount({ contracts: [testContract], runs: [{ ...verificationRun, status: 'inconclusive', stage: 'finished', cleanup_done: false }] })
    expect(screen.getByRole('button', { name: '等待已有验证及清理结束' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '重试清理' }))
    expect(api.cancelVerification).toHaveBeenCalledWith(verificationRun.id, expect.any(String))
    expect(api.createVerification).not.toHaveBeenCalled()
  })
})
