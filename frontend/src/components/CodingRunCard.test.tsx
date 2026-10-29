import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { codingRun } from '../test/coding-fixtures'
import { CodingRunCard } from './CodingRunCard'

describe('coding assistant evidence', () => {
  it('shows real command exit codes separately from model completion and self-printed claims', () => {
    render(<CodingRunCard run={{ ...codingRun, status: 'succeeded', stage: 'finished', cleanup_done: true,
      summary: '我已完成这次修改，请检查生成的文件。', commands: [{ id: 'cmd-1', command: 'python main.py', exit_code: 1,
        stdout: '用户程序打印：所有测试通过', stderr: 'AssertionError: expected 3', status: 'finished', started_at: codingRun.created_at, finished_at: codingRun.updated_at }],
    }} sourceLabel="已保存的正文" />)
    expect(screen.getByText('编码助手已完成')).toBeVisible()
    expect(screen.getByText('退出码 1')).toBeVisible()
    expect(screen.getByText('用户程序打印：所有测试通过')).toBeVisible()
    expect(screen.getByText('AssertionError: expected 3')).toBeVisible()
    expect(screen.getByText(/均不直接判定作业通过/)).toBeVisible()
    expect(screen.queryByText('符合本次要求')).not.toBeInTheDocument()
    expect(screen.getByText('工作区已销毁')).toBeVisible()
  })

  it('never invents exit code zero when the command has not returned a code', () => {
    const command = { id: 'cmd-1', command: 'python main.py', exit_code: null, stdout: '', stderr: '', status: 'running' as const, started_at: codingRun.created_at, finished_at: null }
    const view = render(<CodingRunCard run={{ ...codingRun, commands: [command] }} sourceLabel="main.py" />)
    expect(screen.getByText('命令仍在运行，尚无退出码')).toBeVisible()
    expect(screen.queryByText('退出码 0')).not.toBeInTheDocument()
    view.rerender(<CodingRunCard run={{ ...codingRun, commands: [{ ...command, status: 'failed' }] }} sourceLabel="main.py" />)
    expect(screen.getByText('本次未返回退出码')).toBeVisible()
    expect(screen.queryByText('退出码 0')).not.toBeInTheDocument()
    view.rerender(<CodingRunCard run={{ ...codingRun, commands: [{ ...command, status: 'finished', exit_code: 0 }] }} sourceLabel="main.py" />)
    expect(screen.getByText('退出码 0')).toBeVisible()
  })

  it('keeps generated files as downloads, escapes logs, and refuses cross-run or external artifact links', () => {
    const { container } = render(<CodingRunCard run={{ ...codingRun,
      instruction: '<img src="https://tracker.example/pixel">', summary: '<script>alert(1)</script>',
      commands: [{ id: 'cmd-1', command: '<script>alert(2)</script>', exit_code: 0, stdout: '<iframe src="https://tracker.example">', stderr: '', status: 'finished', started_at: codingRun.created_at, finished_at: codingRun.updated_at }],
      artifacts: [
        { id: 'file-1', filename: 'main.py', size: 256, sha256: 'a'.repeat(64), download_url: '/api/v1/coding-runs/coding-1/artifacts/file-1' },
        { id: 'file-2', filename: 'foreign.py', size: 12, sha256: 'b'.repeat(64), download_url: '/api/v1/coding-runs/another/artifacts/file-2' },
        { id: 'file-3', filename: 'remote.py', size: 12, sha256: 'c'.repeat(64), download_url: 'https://external.example/code.py' },
      ],
    }} sourceLabel="已保存的正文" />)
    expect(container.querySelector('script,img,iframe')).toBeNull()
    expect(screen.getAllByRole('link')).toHaveLength(1)
    expect(screen.getByRole('link', { name: 'main.py' })).toHaveAttribute('download', 'main.py')
    expect(screen.getByRole('link', { name: 'main.py' })).toHaveAttribute('href', '/api/v1/coding-runs/coding-1/artifacts/file-1')
    expect(screen.queryByRole('button', { name: /提交/ })).not.toBeInTheDocument()
  })

  it('keeps a workspace during a run, then reports cleanup only from its actual confirmation', () => {
    const view = render(<CodingRunCard run={codingRun} sourceLabel="main.py" />)
    expect(screen.getByText('工作区保留至本次任务结束')).toBeVisible()
    view.rerender(<CodingRunCard run={{ ...codingRun, stage: 'closing' }} sourceLabel="main.py" />)
    expect(screen.getByText('等待工作区清理确认')).toBeVisible()
    view.rerender(<CodingRunCard run={{ ...codingRun, status: 'cancelled', stage: 'finished', cleanup_done: true }} sourceLabel="main.py" />)
    expect(screen.getByText('工作区已销毁')).toBeVisible()
    view.rerender(<CodingRunCard run={{ ...codingRun, status: 'unavailable', workspace_created: false }} sourceLabel="main.py" />)
    expect(screen.getByText('尚未确认工作区创建')).toBeVisible()
    expect(screen.queryByText('工作区已销毁')).not.toBeInTheDocument()
    view.rerender(<CodingRunCard run={{ ...codingRun, status: 'unavailable', workspace_created: false, cleanup_done: true }} sourceLabel="main.py" />)
    expect(screen.getByText('清理已确认，无需保留工作区')).toBeVisible()
    expect(screen.queryByText('未创建工作区')).not.toBeInTheDocument()
    expect(screen.queryByText('工作区已销毁')).not.toBeInTheDocument()
  })

  it('stops only after a user action and disables repeated cancellation while stopping', async () => {
    const stop = vi.fn()
    const user = userEvent.setup()
    const view = render(<CodingRunCard run={codingRun} sourceLabel="main.py" onCancel={stop} />)
    expect(stop).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '停止编码助手' }))
    expect(stop).toHaveBeenCalledOnce()
    view.rerender(<CodingRunCard run={{ ...codingRun, status: 'cancelling' }} sourceLabel="main.py" onCancel={stop} />)
    expect(screen.getByRole('button', { name: '正在停止…' })).toBeDisabled()
  })

  it('shows the model frozen for a queued run before any model requests have occurred', () => {
    render(<CodingRunCard run={{ ...codingRun, status: 'accepted', stage: 'queued', workspace_created: false,
      usage: { model_name: '启动时选择的模型', model_id: 'frozen-model', requests: 0, input_tokens: 0, output_tokens: 0,
        total_tokens: 0, usage_complete: true, estimated_cost_cny: '0', cost_complete: true, pricing_known: true },
    }} sourceLabel="已保存的正文" />)
    expect(screen.getByText('本次固定模型：启动时选择的模型')).toBeVisible()
    expect(screen.getByText('等待编码助手')).toBeVisible()
    expect(screen.queryByLabelText('模型用量')).not.toBeInTheDocument()
  })
})
