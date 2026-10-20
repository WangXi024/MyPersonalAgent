import { ArrowRight, Check, LoaderCircle, Pencil, Sprout } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { Accepted, GoalDraft, GoalInput, Operation } from '../api/types'
import { useAction } from '../hooks'
import { useApp } from '../state'
import { ErrorNotice, OperationCard } from './ui'

export function GoalDraftCard({ proposal }: { proposal: GoalDraft }) {
  const { data, revision, refresh } = useApp()
  const { pending, error, run } = useAction()
  const [editing, setEditing] = useState(false)
  const [form, setForm] = useState({ ...proposal.draft, criteria: proposal.draft.criteria.join('\n'), deadline: proposal.draft.deadline ?? '' })
  const [validation, setValidation] = useState<Error | null>(null)
  const [accepted, setAccepted] = useState<{ response: Accepted; draft: GoalInput } | null>(null)
  const [loadedOperation, setLoadedOperation] = useState<Operation | null>(null)
  const [operationError, setOperationError] = useState<Error | null>(null)
  const isAccepted = proposal.status === 'accepted' || accepted !== null
  const expired = proposal.status === 'superseded' && !isAccepted
  const goalId = proposal.goal_id ?? accepted?.response.resource_id
  const planId = proposal.plan_operation_id ?? accepted?.response.operation_id
  const snapshotOperation = data?.operations.find(operation => operation.id === planId)
  const operation = snapshotOperation ?? (loadedOperation?.id === planId ? loadedOperation : null)
  useEffect(() => {
    if (!planId || snapshotOperation) return
    let current = true
    void api.operation(planId).then(value => { if (current) { setLoadedOperation(value); setOperationError(null) } }).catch(err => { if (current) setOperationError(err) })
    return () => { current = false }
  }, [planId, snapshotOperation, revision])
  const change = (field: keyof typeof form, value: string) => setForm(previous => ({ ...previous, [field]: value }))
  const input: GoalInput = { title: form.title.trim(), background: form.background.trim(), outcome: form.outcome.trim(), criteria: form.criteria.split('\n').map(value => value.trim()).filter(Boolean), deadline: form.deadline || null }
  const display = proposal.status === 'accepted' ? proposal.draft : accepted?.draft ?? input
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (isAccepted || expired || pending) return
    if (!input.title || !input.background || !input.outcome || !input.criteria.length || input.criteria.length > 20 || input.criteria.some(value => value.length > 500)) {
      setValidation(new Error('请填写目标、基础和预期成果，以及 1–20 项完成标准，每项不超过 500 字。'))
      setEditing(true)
      return
    }
    setValidation(null)
    const result = await run(JSON.stringify(input), key => api.acceptGoalDraft(proposal.operation_id, input, key))
    if (result) { setAccepted({ response: result, draft: input }); setEditing(false) }
  }
  return <form className={`goal-draft-card ${expired ? 'goal-draft-expired' : ''}`} onSubmit={submit} aria-label={`目标草稿：${display.title}`}>
    <div className="row spread wrap"><span className="row goal-draft-label">{isAccepted ? <Check size={15} /> : <Sprout size={16} />}{isAccepted ? '目标已创建' : expired ? '旧目标草稿' : '待你确认的目标'}</span>{!isAccepted && !expired && <button type="button" className="text-button" disabled={pending} aria-expanded={editing} onClick={() => setEditing(value => !value)}><Pencil size={13} />{editing ? '收起修改' : '修改'}</button>}</div>
    {editing && !isAccepted && !expired ? <div className="goal-draft-fields">
      <label>学习目标<input required maxLength={200} disabled={pending} value={form.title} onChange={event => change('title', event.target.value)} /></label>
      <label>现在的基础<textarea required rows={2} maxLength={8000} disabled={pending} value={form.background} onChange={event => change('background', event.target.value)} /></label>
      <label>预期成果<textarea required rows={2} maxLength={8000} disabled={pending} value={form.outcome} onChange={event => change('outcome', event.target.value)} /></label>
      <label>完成标准<small>每行一项，最多 20 项，每项不超过 500 字。</small><textarea required rows={3} maxLength={10019} disabled={pending} value={form.criteria} onChange={event => change('criteria', event.target.value)} /></label>
      <label>希望完成的日期<small>选填，可保留空白。</small><input type="date" disabled={pending} value={form.deadline} onChange={event => change('deadline', event.target.value)} /></label>
    </div> : <div className="goal-draft-summary"><h3>{display.title}</h3><dl><dt>已有基础</dt><dd className="preserve-text">{display.background}</dd><dt>预期成果</dt><dd className="preserve-text">{display.outcome}</dd><dt>完成标准</dt><dd><ul>{display.criteria.map((criterion, index) => <li key={index}>{criterion}</li>)}</ul></dd><dt>期望日期</dt><dd>{display.deadline ?? '暂不设定'}</dd></dl></div>}
    {isAccepted ? <div className="goal-draft-created"><p role="status">目标已保存，计划仍需你确认后才开始推进。</p>{operation ? <OperationCard operation={operation} /> : <p className="muted">正在读取计划准备状态…</p>}<ErrorNotice error={snapshotOperation ? null : operationError} retry={() => void refresh()} />{goalId && <Link className="button secondary" to={`/goals/${goalId}`}>查看目标与计划<ArrowRight size={14} /></Link>}</div> : expired ? <p className="goal-draft-note">这份草稿已失效，请查看对话中的最新草稿。</p> : <><p className="goal-draft-budget">每天 {data?.user.daily_minutes ?? '—'} 分钟由所有目标共享，沿用当前设置。</p><p className="goal-draft-note">确认后创建并准备计划，计划仍需确认。</p><ErrorNotice error={validation ?? error} /><button className="button primary" disabled={pending}>{pending ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}{pending ? '正在创建目标' : '确认创建目标'}</button></>}
  </form>
}
