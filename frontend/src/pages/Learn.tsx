import { ArrowLeft, Check, Clock3, FileText, LoaderCircle, Paperclip, Play, Send, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import type { FormEvent, SetStateAction } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { api, newRequestKey } from '../api/client'
import { useAction, useGoal } from '../hooks'
import { Conversation } from '../components/Conversation'
import { EmptyState, ErrorNotice, Loading, PageHeading, Status, timeLabel } from '../components/ui'
import { EvidenceType } from './GoalDetail'

export function Learn() {
  const { goalId = '' } = useParams()
  const [params, setParams] = useSearchParams()
  const { detail, error, reload } = useGoal(goalId)
  const taskAction = useAction()
  const submissionAction = useAction()
  const uploadAction = useAction()
  const [drafts, setDrafts] = useState<Record<string, { text: string; minutes: string; files: { id: string; filename: string }[] }>>({})
  const [queuedUpload, setQueuedUpload] = useState<{ taskId: string; file: File } | null>(null)
  const [submissionSaved, setSubmissionSaved] = useState(false)
  const [deferDate, setDeferDate] = useState('')
  const [showDefer, setShowDefer] = useState(false)
  const attempts = useRef<Record<string, string>>({})
  const selectedTask = params.has('task') ? detail?.tasks.find(task => task.id === params.get('task')) : detail?.tasks.find(task => ['in_progress', 'ready', 'needs_revision', 'awaiting_review'].includes(task.status)) ?? detail?.tasks[0]
  const taskKey = selectedTask?.id ?? ''
  const submission = drafts[taskKey]?.text ?? ''
  const minutes = drafts[taskKey]?.minutes ?? ''
  const files = drafts[taskKey]?.files ?? []
  const setSubmission = (text: string) => setDrafts(current => ({ ...current, [taskKey]: { ...(current[taskKey] ?? { text: '', minutes: '', files: [] }), text } }))
  const setMinutes = (minutes: string) => setDrafts(current => ({ ...current, [taskKey]: { ...(current[taskKey] ?? { text: '', minutes: '', files: [] }), minutes } }))
  const setFiles = (value: SetStateAction<{ id: string; filename: string }[]>) => setDrafts(current => ({ ...current, [taskKey]: { ...(current[taskKey] ?? { text: '', minutes: '', files: [] }), files: typeof value === 'function' ? value(current[taskKey]?.files ?? []) : value } }))
  useEffect(() => {
    if (selectedTask && !params.has('task')) { const next = new URLSearchParams(params); next.set('task', selectedTask.id); setParams(next, { replace: true }) }
  }, [selectedTask, params, setParams])
  if (!detail) return error ? <ErrorNotice error={error} retry={reload} /> : <Loading label="正在打开学习空间" />
  const { goal, tasks, evaluations, submissions, operations } = detail
  const active = goal.status === 'active'
  const canChat = !['cancelled', 'completed'].includes(goal.status)
  const taskSubmissions = submissions.filter(item => item.task_id === selectedTask?.id)
  const taskEvaluations = evaluations.filter(evaluation => taskSubmissions.some(item => item.id === evaluation.submission_id))
  const currentEvaluation = taskEvaluations.find(evaluation => evaluation.submission_id === selectedTask?.active_submission_id)
  const commandTask = (verb: string, extra: Record<string, unknown> = {}) => {
    if (!selectedTask) return Promise.resolve(undefined)
    const body = { expected_version: selectedTask.state_version, ...extra }
    return taskAction.run(`${selectedTask.id}/${verb}/${JSON.stringify(body)}`, key => api.command(`/tasks/${selectedTask.id}/${verb}`, body, key))
  }
  const uploadFile = async (file: File) => {
    setQueuedUpload({ taskId: taskKey, file })
    const result = await uploadAction.run(`${taskKey}/${file.name}/${file.size}/${file.lastModified}`, key => api.upload(file, key))
    if (result) { setFiles(current => [...current, result]); setQueuedUpload(current => current?.file === file ? null : current) }
  }
  const submitWork = async (event: FormEvent) => {
    event.preventDefault()
    if (!selectedTask || (!submission.trim() && !files.length)) return
    const body = { text: submission.trim(), attachment_ids: files.map(file => file.id), expected_version: selectedTask.state_version, attempt_id: attempts.current[selectedTask.id] ??= newRequestKey(), minutes: Number(minutes), estimated: false }
    const result = await submissionAction.run(JSON.stringify({ task_id: selectedTask.id, text: body.text, attachment_ids: body.attachment_ids, minutes: body.minutes }), key => api.command(`/tasks/${selectedTask.id}/submissions`, body, key))
    if (result) { setSubmission(''); setFiles([]); setMinutes(''); setSubmissionSaved(true); delete attempts.current[selectedTask.id] }
  }
  return <><Link className="back-link" to={`/goals/${goalId}`}><ArrowLeft size={15} />{goal.title}</Link><PageHeading title="学习空间" description="查看任务、提交练习，与 Agent 讨论问题。" action={<Status value={goal.status} />} /><ErrorNotice error={error || taskAction.error} retry={error ? reload : undefined} /><div className="learning-grid"><section className="task-workspace"><div className="card current-task"><div className="row spread"><span className="eyebrow">当前任务</span>{selectedTask && <Status value={selectedTask.status} />}</div>{tasks.length > 1 && <label className="task-selector">选择任务<select disabled={submissionAction.pending || uploadAction.pending} value={selectedTask?.id ?? ''} onChange={event => { setParams({ task: event.target.value }); setSubmissionSaved(false) }}>{tasks.map(task => <option value={task.id} key={task.id}>{task.title}</option>)}</select></label>}{selectedTask ? <><h2>{selectedTask.title}</h2><div className="task-estimate"><Clock3 size={15} />预计 {selectedTask.estimated_minutes} 分钟{selectedTask.scheduled_date && <span>· {selectedTask.scheduled_date}</span>}</div><p className="preserve-text task-instructions">{selectedTask.instructions}</p><h3>完成这一步的标准</h3><ul className="check-list">{selectedTask.criteria.map((criterion, index) => <li key={index}><span className="small-ring" />{criterion}</li>)}</ul>{selectedTask.skills.length > 0 && <div className="skill-tags">{selectedTask.skills.map(skill => <span key={skill}>{skill}</span>)}</div>}<ErrorNotice error={taskAction.error} />{active && !['completed', 'cancelled', 'skipped', 'awaiting_review'].includes(selectedTask.status) && <div className="task-controls row wrap">{selectedTask.status === 'ready' && <button className="button primary" disabled={taskAction.pending} onClick={() => void commandTask('start')}><Play size={15} />开始学习</button>}<button className="button secondary" onClick={() => setShowDefer(value => !value)}>晚点再学</button><button className="text-button muted" disabled={taskAction.pending} onClick={() => void commandTask('skip')}>跳过这项任务</button></div>}{showDefer && <form className="defer-form row" onSubmit={event => { event.preventDefault(); void commandTask('defer', { scheduled_date: deferDate }).then(result => { if (result) setShowDefer(false) }) }}><label>安排到<input type="date" required value={deferDate} onChange={event => setDeferDate(event.target.value)} /></label><button className="button secondary" disabled={taskAction.pending}>确认改期</button></form>}</> : <EmptyState title="请先确认学习计划" action={<Link className="button secondary" to={`/goals/${goalId}`}>查看目标计划</Link>}>可在对话中继续讨论学习方向。</EmptyState>}</div>
      {selectedTask && active && !['completed', 'skipped', 'cancelled'].includes(selectedTask.status) && <form className="card submission-form" onSubmit={submitWork}><div className="row spread"><h2>提交你的练习</h2><FileText size={19} /></div><p className="muted">保存你的实际尝试，Agent 会依据任务要求提供反馈。</p>{submissionSaved && <div className="notice success" role="status"><Check size={17} /><span>提交已保存，正在等待评估。结果会保存在这里。</span></div>}<ErrorNotice error={submissionAction.error || uploadAction.error} /><label>答案、代码或学习成果<textarea disabled={submissionAction.pending} required={!files.length} rows={7} maxLength={30000} placeholder="写下你的解答、代码或尝试过程。遇到困难也可以如实记录。" value={submission} onChange={event => setSubmission(event.target.value)} /></label><div className="attachment-list">{files.map(file => <span className="attachment-chip" key={file.id}><Paperclip size={13} />{file.filename}<button type="button" disabled={submissionAction.pending} aria-label={`移除 ${file.filename}`} onClick={() => setFiles(current => current.filter(item => item.id !== file.id))}><X size={13} /></button></span>)}</div><div className="row spread wrap"><label className="upload-button"><Paperclip size={15} />{uploadAction.pending ? '上传中…' : files.length >= 5 ? '已添加 5 个附件' : '添加附件'}<input type="file" accept=".txt,.md,.pdf,.py,.js,.ts,.json,.csv" disabled={uploadAction.pending || submissionAction.pending || files.length >= 5} onChange={event => { const file = event.target.files?.[0]; if (file) void uploadFile(file); event.target.value = '' }} /></label><label className="minutes-field">本次实际投入<input disabled={submissionAction.pending} required type="number" min={0} max={1440} value={minutes} onChange={event => setMinutes(event.target.value)} />分钟</label></div>{queuedUpload?.taskId === taskKey && !uploadAction.pending && uploadAction.error && <div className="upload-retry"><span>{queuedUpload.file.name} 尚未上传成功</span><button type="button" className="text-button" onClick={() => void uploadFile(queuedUpload.file)}>重试上传</button><button type="button" className="text-button muted" onClick={() => setQueuedUpload(null)}>移除未上传文件</button></div>}<button className="button primary full-width" disabled={submissionAction.pending || uploadAction.pending || queuedUpload?.taskId === taskKey}>{submissionAction.pending ? <LoaderCircle size={16} className="spin" /> : <Send size={16} />}保存并请求评估</button><small className="submission-footnote">代码不会在服务器执行。AI 审阅与程序验证会明确区分。</small></form>}
      {currentEvaluation && <section className="card feedback-card"><div className="row spread"><h2>这次练习的反馈</h2><span className={`status status-${currentEvaluation.passed ? 'completed' : 'needs_revision'}`}>{currentEvaluation.passed ? '符合本次要求' : '需要继续巩固'}</span></div><EvidenceType type={currentEvaluation.evidence_type} /><p className="preserve-text">{currentEvaluation.feedback}</p>{currentEvaluation.evidence_type === 'ai_review' && <div className="evidence-limitation">{typeof currentEvaluation.details.limitations === 'string' ? currentEvaluation.details.limitations : '这是 AI 按任务标准进行的审阅，未执行用户代码。'}</div>}{!currentEvaluation.valid && <div className="notice info">这是一份历史评估，不代表当前任务的最新结果。</div>}<small>{timeLabel(currentEvaluation.created_at)}</small></section>}
      {taskSubmissions.length > 0 && <section className="card submission-history"><h2>已保存的尝试</h2>{[...taskSubmissions].sort((a, b) => b.created_at.localeCompare(a.created_at)).map(item => <details key={item.id}><summary>查看提交 · {timeLabel(item.created_at)}</summary><pre>{item.text || '本次通过附件提交。'}</pre>{item.attachment_ids?.map(id => <a key={id} href={`/api/v1/attachments/${encodeURIComponent(id)}/download`} className="text-button">下载附件</a>)}</details>)}</section>}
    </section><Conversation conversationId={detail.conversation.id} goalId={goalId} taskId={selectedTask?.id} operations={operations} canSend={canChat} activeGoal={active} initialMessage={params.has('adjust') ? '今天的时间有变化，想调整一下学习任务。' : ''} /></div></>
}
