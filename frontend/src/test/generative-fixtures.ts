import type { ProgressBlock, QuizAttempt, QuizBlock } from '../api/types'

export const quizBlock: QuizBlock = { type: 'quiz', id: 'display-1', operation_id: 'tutor-1', quiz_id: 'quiz-1', title: '函数小练习', practice_only: true, attempt: null, attempt_count: 0, max_attempts: 5,
  questions: [{ id: 'q1', prompt: '哪个关键字把结果交给调用者？', choices: [{ id: 'a', text: 'print' }, { id: 'b', text: 'return' }] },
    { id: 'q2', prompt: '2 + 3 的结果是什么？', choices: [{ id: 'a', text: '5' }, { id: 'b', text: '23' }] }] }
export const quizAttempt: QuizAttempt = { id: 'attempt-1', quiz_id: 'quiz-1', attempt_number: 1, submitted_at: '2026-10-29T03:00:00Z', practice_only: true,
  answers: [{ question_id: 'q1', choice_id: 'a' }, { question_id: 'q2', choice_id: 'a' }], correct_count: 1, total: 2,
  feedback: [{ question_id: 'q1', selected_choice_id: 'a', correct_choice_id: 'b', correct: false, explanation: 'return 返回结果，print 只负责打印。' },
    { question_id: 'q2', selected_choice_id: 'a', correct_choice_id: 'a', correct: true, explanation: '这里执行整数加法。' }] }
export const progressBlock: ProgressBlock = { type: 'progress', id: 'display-2', title: '本次学习记录', snapshot: { goal_id: 'goal-1', title: '学习 Python', goal_status: 'active', goal_revision: 2, plan_id: 'plan-1', as_of: '2026-10-29T02:30:00Z',
  tasks: { total: 7, completed: 2, in_progress: 1, awaiting_review: 1, needs_revision: 1, ready: 1, skipped: 1 }, skills: { total: 3, needs_review: 1 }, evidence: { ai_review: 2, verified_tests: 0 } } }
