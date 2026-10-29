import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { SandboxExecution } from '../api/types'
import { SandboxExecutionCard } from './SandboxExecutionCard'

const execution: SandboxExecution = {
  id: 'exec-1', operation_id: 'op-1', goal_id: 'goal-1', task_id: 'task-1', submission_id: 'submission-1',
  request_id: 'request-1', language: 'python', status: 'succeeded', phase: 'done', outcome: 'success',
  error_code: null, error_message: null, stdout: '所有测试通过', stderr: '', cleanup_done: true, delivery_done: true,
  created_at: '2026-10-29T02:00:00Z', updated_at: '2026-10-29T02:00:10Z', artifacts: [],
}

describe('sandbox execution evidence', () => {
  it('shows a finished script and its printed output without claiming the assignment passed', () => {
    render(<SandboxExecutionCard execution={execution} sourceLabel="已保存的正文" timezone="Asia/Shanghai" />)
    expect(screen.getByText('运行已结束')).toBeVisible()
    expect(screen.getByText('所有测试通过')).toBeVisible()
    expect(screen.getByText(/不判定作业是否通过/)).toBeVisible()
    expect(screen.queryByText('符合本次要求')).not.toBeInTheDocument()
    expect(screen.queryByText('作业通过', { exact: true })).not.toBeInTheDocument()
    expect(screen.getByText('隔离环境已清理')).toBeVisible()
    expect(screen.getByText('结果回执已确认')).toBeVisible()
    expect(screen.queryByRole('button', { name: '取消运行' })).not.toBeInTheDocument()
  })

  it('renders stdout and stderr as text and only links artifacts from the matching saved execution', () => {
    const { container } = render(<SandboxExecutionCard sourceLabel="main.py" execution={{ ...execution,
      stdout: '<img src="https://tracker.example/pixel" onerror="alert(1)">', stderr: '<script>alert(2)</script>',
      artifacts: [
        { id: 'a1', filename: 'result.csv', size: 42, sha256: 'a'.repeat(64), download_url: '/api/v1/sandbox-executions/exec-1/artifacts/a1' },
        { id: 'a2', filename: 'foreign.csv', size: 12, sha256: 'b'.repeat(64), download_url: '/api/v1/sandbox-executions/another-execution/artifacts/a2' },
        { id: 'a3', filename: 'external.csv', size: 12, sha256: 'c'.repeat(64), download_url: 'https://external.example/file' },
      ],
    }} />)
    expect(container.querySelector('script,img,iframe')).toBeNull()
    const link = screen.getByRole('link', { name: 'result.csv' })
    expect(link).toHaveAttribute('href', '/api/v1/sandbox-executions/exec-1/artifacts/a1')
    expect(link).toHaveAttribute('download', 'result.csv')
    expect(screen.getAllByRole('link')).toHaveLength(1)
    expect(screen.getByText('42 字节')).toBeVisible()
  })

  it('does not confirm cleanup from a successful outcome and only cancels after a click', async () => {
    const cancel = vi.fn()
    const user = userEvent.setup()
    const view = render(<SandboxExecutionCard execution={{ ...execution, status: 'running', phase: 'running', cleanup_done: false, delivery_done: false, stdout: '' }} sourceLabel="main.py" onCancel={cancel} />)
    expect(screen.getByText('等待隔离环境清理确认')).toBeVisible()
    expect(screen.getByText('结果回执待确认')).toBeVisible()
    expect(cancel).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '取消运行' }))
    expect(cancel).toHaveBeenCalledOnce()
    view.rerender(<SandboxExecutionCard execution={{ ...execution, status: 'cancelling', cleanup_done: false, delivery_done: false }} sourceLabel="main.py" onCancel={cancel} />)
    expect(screen.getByRole('button', { name: '正在取消…' })).toBeDisabled()
    expect(screen.queryByText('隔离环境已清理')).not.toBeInTheDocument()
  })

  it.each(['failed', 'unavailable', 'expired', 'superseded', 'cancelled'] as const)('preserves actual %s state without replacing it with successful execution', status => {
    render(<SandboxExecutionCard execution={{ ...execution, status, outcome: 'unknown', error_message: '本次运行未完成', stdout: '' }} sourceLabel="已保存的正文" onCancel={vi.fn()} />)
    expect(screen.getByText('本次运行未完成')).toBeVisible()
    expect(screen.queryByText('运行已结束')).not.toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('converts reported CPU microseconds and memory bytes without implying a final report early', () => {
    const view = render(<SandboxExecutionCard execution={{ ...execution, resource_usage: {
      cpu_usage_usec: 1250000, peak_memory_bytes: 13107200, cpu_usage_available: true, peak_memory_available: true, resource_usage_final: false,
    } }} sourceLabel="已保存的正文" />)
    expect(screen.getByText('累计 CPU 1.25 秒')).toBeVisible()
    expect(screen.getByText('峰值内存 12.5 MiB')).toBeVisible()
    expect(screen.getByText('执行中报告')).toBeVisible()
    view.rerender(<SandboxExecutionCard execution={{ ...execution, resource_usage: {
      cpu_usage_usec: 1250000, peak_memory_bytes: 13107200, cpu_usage_available: true, peak_memory_available: true, resource_usage_final: true,
    } }} sourceLabel="已保存的正文" />)
    expect(screen.queryByText('执行中报告')).not.toBeInTheDocument()
  })

  it('hides unavailable metrics, including protobuf default zeroes, and supports partial reports', () => {
    const view = render(<SandboxExecutionCard execution={{ ...execution, resource_usage: {
      cpu_usage_usec: 0, peak_memory_bytes: 0, cpu_usage_available: false, peak_memory_available: false, resource_usage_final: false,
    } }} sourceLabel="已保存的正文" />)
    expect(screen.queryByLabelText('运行资源报告')).not.toBeInTheDocument()
    view.rerender(<SandboxExecutionCard execution={{ ...execution, resource_usage: {
      cpu_usage_usec: 0, peak_memory_bytes: 1048576, cpu_usage_available: false, peak_memory_available: true, resource_usage_final: true,
    } }} sourceLabel="已保存的正文" />)
    expect(screen.getByText('峰值内存 1 MiB')).toBeVisible()
    expect(screen.queryByText(/累计 CPU/)).not.toBeInTheDocument()
    view.rerender(<SandboxExecutionCard execution={execution} sourceLabel="已保存的正文" />)
    expect(screen.queryByLabelText('运行资源报告')).not.toBeInTheDocument()
  })

  it.each([NaN, Infinity, -Infinity, -1])('does not display nonfinite or negative values even when availability is true: %s', value => {
    render(<SandboxExecutionCard execution={{ ...execution, resource_usage: {
      cpu_usage_usec: value, peak_memory_bytes: value, cpu_usage_available: true, peak_memory_available: true, resource_usage_final: true,
    } }} sourceLabel="已保存的正文" />)
    expect(screen.queryByLabelText('运行资源报告')).not.toBeInTheDocument()
  })

  it('retains measured zeroes only when the service explicitly reports them as available', () => {
    render(<SandboxExecutionCard execution={{ ...execution, resource_usage: {
      cpu_usage_usec: 0, peak_memory_bytes: 0, cpu_usage_available: true, peak_memory_available: true, resource_usage_final: true,
    } }} sourceLabel="已保存的正文" />)
    expect(screen.getByText('累计 CPU 0 秒')).toBeVisible()
    expect(screen.getByText('峰值内存 0 MiB')).toBeVisible()
  })

  it('distinguishes a terminal request never submitted from a queued or historically unknown request', () => {
    const pending = { ...execution, stdout: '', stderr: '', cleanup_done: true, delivery_done: false, submitted_at: null }
    const view = render(<SandboxExecutionCard execution={{ ...pending, status: 'cancelled' }} sourceLabel="已保存的正文" />)
    for (const status of ['cancelled', 'unavailable', 'expired'] as const) {
      view.rerender(<SandboxExecutionCard execution={{ ...pending, status }} sourceLabel="已保存的正文" />)
      expect(screen.getByText('未创建隔离环境')).toBeVisible()
      expect(screen.getByText('无需结果回执')).toBeVisible()
      expect(screen.getByText('本次未执行代码。')).toBeVisible()
      expect(screen.queryByText('隔离环境已清理')).not.toBeInTheDocument()
      expect(screen.queryByText('结果回执待确认')).not.toBeInTheDocument()
      expect(screen.queryByText('程序输出 stdout')).not.toBeInTheDocument()
    }
    view.rerender(<SandboxExecutionCard execution={{ ...pending, status: 'accepted', cleanup_done: false }} sourceLabel="已保存的正文" />)
    expect(screen.getByText('等待隔离环境清理确认')).toBeVisible()
    expect(screen.getByText('结果回执待确认')).toBeVisible()
    expect(screen.queryByText('未创建隔离环境')).not.toBeInTheDocument()
    view.rerender(<SandboxExecutionCard execution={{ ...pending, status: 'cancelled', submitted_at: undefined }} sourceLabel="已保存的正文" />)
    expect(screen.queryByText('未创建隔离环境')).not.toBeInTheDocument()
    expect(screen.getByText('结果回执待确认')).toBeVisible()
  })
})
