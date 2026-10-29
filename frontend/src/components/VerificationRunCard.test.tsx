import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { verifiedRun } from '../test/verification-fixtures'
import { VerificationRunCard } from './VerificationRunCard'
describe('trusted comparison results', () => {
  it('shows complete actual matched cases as user-confirmed sample verification without marking a task or skill mastered', () => {
    render(<VerificationRunCard run={verifiedRun} sourceLabel="已保存的正文" contractVersion={1} />)
    expect(screen.getByText('已确认样例验证通过')).toBeVisible()
    expect(screen.getByText('符合确认规则')).toBeVisible()
    expect(screen.getByText(/不自动完成任务或更新知识掌握记录/)).toBeVisible()
    expect(screen.queryByText('作业通过')).not.toBeInTheDocument()
  })
  it.each([{ exit_code: 1 }, { output_complete: false }, { output_truncated: true }, { utf8_valid: false }, { matched: false }, { timed_out: true }])('refuses a passed label for incomplete or contradictory native evidence %j', patch => {
    render(<VerificationRunCard run={{ ...verifiedRun, cases: [{ ...verifiedRun.cases[0], ...patch, stdout: 'passed 所有测试通过' }] }} sourceLabel="main.py" />)
    expect(screen.queryByText('已确认样例验证通过')).not.toBeInTheDocument()
    expect(screen.getByText('passed 所有测试通过')).toBeInTheDocument()
    expect(screen.getByText('验证结论尚待确认')).toBeVisible()
  })
  it('does not fabricate an exit code or execute markup from stdout and stderr', () => {
    const { container } = render(<VerificationRunCard run={{ ...verifiedRun, status: 'inconclusive', tests_verified: false, cases: [{ ...verifiedRun.cases[0], status: 'inconclusive', exit_code: null, stdout: '<script>alert(1)</script>', stderr: '<img src=x>', output_complete: false, matched: null }] }} sourceLabel="main.py" />)
    expect(screen.getByText(/本例未返回可信退出码/)).toBeVisible()
    expect(screen.queryByText('实际退出码 0')).not.toBeInTheDocument()
    expect(container.querySelector('script,img,iframe')).toBeNull()
  })
})
