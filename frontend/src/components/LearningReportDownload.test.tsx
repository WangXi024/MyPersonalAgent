import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../api/client'
import { LearningReportDownload } from './LearningReportDownload'
const create = vi.fn(() => 'blob:local-report'), revoke = vi.fn()
const OriginalURL = URL
beforeEach(() => {
  create.mockClear(); revoke.mockClear()
  vi.stubGlobal('URL', class extends OriginalURL { static createObjectURL = create; static revokeObjectURL = revoke })
  vi.spyOn(api, 'learningReport').mockResolvedValue({ blob: new Blob(['# 学习报告'], { type: 'text/markdown' }), filename: 'learning-report-goal-1.md' })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
})
afterEach(() => { vi.unstubAllGlobals() })
describe('authenticated learning report download', () => {
  it('downloads only after a click and releases its local blob URL when leaving', async () => {
    const user = userEvent.setup(), view = render(<LearningReportDownload goalId="goal-1" />)
    expect(api.learningReport).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '下载学习报告' }))
    await waitFor(() => expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledOnce())
    expect(api.learningReport).toHaveBeenCalledWith('goal-1', expect.any(AbortSignal))
    const link = vi.mocked(HTMLAnchorElement.prototype.click).mock.instances[0] as unknown as HTMLAnchorElement
    expect(link.href).toBe('blob:local-report'); expect(link.download).toBe('learning-report-goal-1.md')
    view.unmount()
    expect(revoke).toHaveBeenCalledWith('blob:local-report')
  })
  it('shows an authentication error without downloading an error page or fabricating a report', async () => {
    vi.mocked(api.learningReport).mockRejectedValue(new ApiError(401, '登录已过期，请重新登录。'))
    render(<LearningReportDownload goalId="goal-1" />)
    fireEvent.click(screen.getByRole('button', { name: '下载学习报告' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('登录已过期')
    expect(create).not.toHaveBeenCalled(); expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: '下载学习报告' })).toBeEnabled()
  })
  it('aborts an in-flight download and ignores a late response after leaving the goal', async () => {
    let finish: (value: { blob: Blob; filename: string }) => void = () => undefined
    vi.mocked(api.learningReport).mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const view = render(<LearningReportDownload goalId="goal-1" />)
    fireEvent.click(screen.getByRole('button', { name: '下载学习报告' }))
    expect(screen.getByRole('button', { name: '下载学习报告' })).toBeDisabled()
    const signal = vi.mocked(api.learningReport).mock.calls[0][1]!
    view.unmount(); expect(signal.aborted).toBe(true)
    finish({ blob: new Blob(['late']), filename: 'learning-report.md' })
    await Promise.resolve()
    expect(create).not.toHaveBeenCalled()
  })
})
