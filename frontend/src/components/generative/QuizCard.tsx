import { useId, useState } from 'react'
import type { FormEvent } from 'react'
import { ClipboardCheck, LoaderCircle } from 'lucide-react'
import { api } from '../../api/client'
import type { QuizAttempt, QuizBlock } from '../../api/types'
import { useAction } from '../../hooks'
import { ErrorNotice } from '../ui'
import { validQuizAttempt } from './blocks'

export function QuizCard({ block, syncing = false }: { block: QuizBlock; syncing?: boolean }) {
  const group = useId()
  const action = useAction()
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [localAttempt, setLocalAttempt] = useState<QuizAttempt | null>(null)
  const [retaking, setRetaking] = useState(false)
  const attempt = localAttempt && localAttempt.attempt_number >= (block.attempt?.attempt_number ?? 0) ? localAttempt : block.attempt
  const count = Math.max(block.attempt_count, attempt?.attempt_number ?? 0)
  const practicing = !attempt || retaking
  const selected = practicing ? answers : Object.fromEntries(attempt.answers.map(item => [item.question_id, item.choice_id]))
  const complete = block.questions.every(question => question.choices.some(choice => choice.id === answers[question.id]))
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!complete || count >= block.max_attempts || syncing) return
    const payload = block.questions.map(question => ({ question_id: question.id, choice_id: answers[question.id] }))
    const result = await action.run(`quiz/${block.operation_id}/${block.quiz_id}/${JSON.stringify(payload)}`, async key => {
      const value = await api.submitQuizAttempt(block.operation_id, block.quiz_id, payload, key)
      if (!validQuizAttempt(value, block.questions, block.quiz_id)) throw new Error('练习反馈格式暂不可用，请刷新后重试。')
      return value
    })
    if (result) { setLocalAttempt(result); setRetaking(false) }
  }
  return <section className="genui-card genui-quiz" aria-label={`自测练习：${block.title}`}>
    <header className="genui-heading"><ClipboardCheck size={16} aria-hidden="true" /><h3>{block.title}</h3><span>仅供练习</span></header>
    <p className="genui-footnote">选择后点击检查答案，才会显示反馈。练习结果不改变任务完成状态或知识掌握记录。</p>
    <form onSubmit={submit}>
      {block.questions.map((question, index) => {
        const feedback = !practicing ? attempt?.feedback.find(item => item.question_id === question.id) : undefined
        return <fieldset key={question.id} className="genui-quiz-question" disabled={!practicing || action.pending || syncing || count >= block.max_attempts}>
          <legend><span>第 {index + 1} 题</span>{question.prompt}</legend>
          {question.choices.map(choice => <label key={choice.id}><input type="radio" name={`${group}-${question.id}`} value={choice.id} checked={selected[question.id] === choice.id} onChange={() => { setRetaking(true); setAnswers(previous => ({ ...previous, [question.id]: choice.id })) }} /><span>{choice.text}</span></label>)}
          {feedback && <div className={`genui-quiz-feedback${feedback.correct ? ' correct' : ''}`}><strong>{feedback.correct ? '这道题答对了' : '这道题可以再想一想'}</strong><p>参考答案：{question.choices.find(choice => choice.id === feedback.correct_choice_id)!.text}</p>{feedback.explanation && <p>{feedback.explanation}</p>}</div>}
        </fieldset>
      })}
      <ErrorNotice error={action.error} />
      {!practicing && attempt && <p className="genui-quiz-result" role="status">本次练习答对 {attempt.correct_count} / {attempt.total} 题，不作为作业评估依据。</p>}
      <div className="genui-quiz-actions">{practicing ? <button className="button secondary" type="submit" disabled={action.pending || syncing || !complete || count >= block.max_attempts}>{action.pending ? <LoaderCircle size={14} className="spin" /> : null}{action.pending ? '正在检查' : '检查答案'}</button> : <button type="button" className="text-button" disabled={syncing || count >= block.max_attempts} onClick={() => { setAnswers({}); setRetaking(true); action.clearError() }}>再练一次</button>}<small>已练习 {count} / {block.max_attempts} 次{count >= block.max_attempts ? ' · 已达本次练习上限' : ''}</small></div>
    </form>
  </section>
}
