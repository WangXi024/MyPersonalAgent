import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../../api/client'
import { quizAttempt, quizBlock } from '../../test/generative-fixtures'
import { QuizCard } from './QuizCard'
const refresh = vi.fn().mockResolvedValue(undefined)
vi.mock('../../state', () => ({ useApp: () => ({ refresh }) }))
beforeEach(() => { vi.spyOn(api, 'submitQuizAttempt').mockResolvedValue(quizAttempt); refresh.mockClear() })
async function choose() {
  const user = userEvent.setup()
  await user.click(screen.getByRole('radio', { name: 'print' }))
  await user.click(screen.getByRole('radio', { name: '5' }))
  return user
}
describe('practice-only quiz', () => {
  it('renders question and feedback markup as text without executing generated HTML', () => {
    const prompt = '<img src="https://tracker.invalid/pixel">', explanation = '<script>fetch("/api/v1/goals")</script>'
    const { container } = render(<QuizCard block={{ ...quizBlock, questions: [{ ...quizBlock.questions[0], prompt }, quizBlock.questions[1]], attempt_count: 1,
      attempt: { ...quizAttempt, feedback: [{ ...quizAttempt.feedback[0], explanation }, quizAttempt.feedback[1]] } }} />)
    expect(screen.getByText(prompt)).toBeVisible(); expect(screen.getByText(explanation)).toBeVisible()
    expect(container.querySelector('img,script,iframe')).toBeNull()
    expect(api.submitQuizAttempt).not.toHaveBeenCalled()
  })
  it('requires complete choices and an explicit check before revealing persisted feedback', async () => {
    render(<QuizCard block={quizBlock} />)
    expect(api.submitQuizAttempt).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: '检查答案' })).toBeDisabled()
    expect(screen.queryByText(/参考答案/)).not.toBeInTheDocument()
    const user = await choose()
    expect(api.submitQuizAttempt).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '检查答案' }))
    expect(api.submitQuizAttempt).toHaveBeenCalledWith('tutor-1', 'quiz-1', quizAttempt.answers, expect.any(String))
    expect(await screen.findByRole('status')).toHaveTextContent('本次练习答对 1 / 2 题，不作为作业评估依据')
    expect(screen.getByText('参考答案：return')).toBeVisible()
    expect(screen.getByText(/练习结果不改变任务完成状态或知识掌握记录/)).toBeVisible()
    expect(screen.queryByText('作业通过')).not.toBeInTheDocument()
  })
  it('restores the latest practice attempt without posting and respects the five-attempt limit', () => {
    render(<QuizCard block={{ ...quizBlock, attempt: { ...quizAttempt, attempt_number: 5 }, attempt_count: 5 }} />)
    expect(screen.getByRole('radio', { name: 'print' })).toBeChecked()
    expect(screen.getByText('参考答案：return')).toBeVisible()
    expect(screen.getByRole('button', { name: '再练一次' })).toBeDisabled()
    expect(api.submitQuizAttempt).not.toHaveBeenCalled()
  })
  it('preserves a failed selection and reuses the same request key on retry', async () => {
    vi.mocked(api.submitQuizAttempt).mockRejectedValueOnce(new ApiError(0, '连接暂时中断')).mockResolvedValue(quizAttempt)
    render(<QuizCard block={quizBlock} />)
    const user = await choose()
    await user.click(screen.getByRole('button', { name: '检查答案' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('连接暂时中断')
    expect(screen.getByRole('radio', { name: 'print' })).toBeChecked()
    await user.click(screen.getByRole('button', { name: '检查答案' }))
    await screen.findByRole('status')
    expect(vi.mocked(api.submitQuizAttempt).mock.calls[0][3]).toBe(vi.mocked(api.submitQuizAttempt).mock.calls[1][3])
  })
  it('requires fresh choices to practice again and never labels the previous answers as new feedback', async () => {
    render(<QuizCard block={{ ...quizBlock, attempt: quizAttempt, attempt_count: 1 }} />)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '再练一次' }))
    expect(screen.queryByText(/参考答案/)).not.toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'print' })).not.toBeChecked()
    expect(screen.getByRole('button', { name: '检查答案' })).toBeDisabled()
    expect(api.submitQuizAttempt).not.toHaveBeenCalled()
  })
  it('rejects feedback for different questions or invented counts and keeps content as text', async () => {
    vi.mocked(api.submitQuizAttempt).mockResolvedValue({ ...quizAttempt, correct_count: 2 })
    render(<QuizCard block={quizBlock} />)
    const user = await choose()
    await user.click(screen.getByRole('button', { name: '检查答案' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('练习反馈格式暂不可用')
    expect(screen.queryByText(/参考答案/)).not.toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
  it('stops duplicate checks while the first persisted request is pending', async () => {
    let finish: (value: typeof quizAttempt) => void = () => undefined
    vi.mocked(api.submitQuizAttempt).mockImplementation(() => new Promise(resolve => { finish = resolve }))
    render(<QuizCard block={quizBlock} />)
    const user = await choose()
    await user.click(screen.getByRole('button', { name: '检查答案' }))
    expect(screen.getByRole('button', { name: '正在检查' })).toBeDisabled()
    for (const field of screen.getAllByRole('group')) expect(within(field).getAllByRole('radio').every(radio => (radio as HTMLInputElement).disabled || radio.closest('fieldset')?.disabled)).toBe(true)
    finish(quizAttempt)
    await waitFor(() => expect(screen.getByRole('status')).toBeVisible())
    expect(api.submitQuizAttempt).toHaveBeenCalledOnce()
  })
})
