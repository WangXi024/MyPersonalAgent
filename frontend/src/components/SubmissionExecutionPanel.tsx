import { useState } from 'react'
import { LoaderCircle, Play } from 'lucide-react'
import { api } from '../api/client'
import type { Goal, SandboxExecution, SandboxExecutionInput, Submission, Task } from '../api/types'
import { useAction } from '../hooks'
import { ErrorNotice } from './ui'
import { runningExecution, SandboxExecutionCard } from './SandboxExecutionCard'

export function SubmissionExecutionPanel({ submission, goal, task, executions, timezone }: {
  submission: Submission; goal: Goal; task: Task; executions: SandboxExecution[]; timezone?: string
}) {
  const run = useAction()
  const cancel = useAction()
  const [selection, setSelection] = useState('text')
  const [received, setReceived] = useState<SandboxExecution | null>(null)
  const records = new Map(executions.filter(item => item.submission_id === submission.id && item.goal_id === goal.id && item.task_id === task.id).map(item => [item.id, item]))
  if (received?.submission_id === submission.id && (!records.has(received.id) || received.updated_at > records.get(received.id)!.updated_at)) records.set(received.id, received)
  const saved = [...records.values()].sort((a, b) => b.created_at.localeCompare(a.created_at))
  const files = submission.attachments?.filter(item => /\.py$/i.test(item.filename)) ?? []
  const source = selection === 'text' && !submission.text.trim() && files.length ? files[0].id : selection
  const validSource = source === 'text' ? !!submission.text.trim() : files.some(item => item.id === source)
  const current = submission.task_id === task.id && goal.status === 'active' && goal.active_plan_id === task.plan_id
    && task.active_submission_id === submission.id && !['cancelled', 'skipped'].includes(task.status)
  const active = saved.some(item => runningExecution(item.status))
  const execute = async () => {
    if (!current || !validSource || active) return
    const body: SandboxExecutionInput = { expected_version: task.state_version, expected_goal_revision: goal.control_revision, language: 'python', attachment_id: source === 'text' ? null : source }
    const value = await run.run(`${submission.id}/${JSON.stringify(body)}`, key => api.runSubmission(submission.id, body, key))
    if (value) setReceived(value)
  }
  const stop = async (item: SandboxExecution) => {
    const value = await cancel.run(`cancel/${item.id}`, key => api.cancelSandboxExecution(item.id, key))
    if (value) setReceived(value)
  }
  return <div className="submission-execution-panel">
    {current ? <><div className="sandbox-run-controls row wrap">{files.length > 0 && <label>运行内容<select aria-label="选择已保存的Python代码" disabled={run.pending || active} value={source} onChange={event => setSelection(event.target.value)}>{submission.text.trim() && <option value="text">已保存的正文</option>}{files.map(file => <option key={file.id} value={file.id}>{file.filename}</option>)}</select></label>}
      <button type="button" className="button secondary" disabled={!validSource || run.pending || active} onClick={() => void execute()}>{run.pending ? <LoaderCircle size={14} className="spin" /> : <Play size={14} />}{active ? '已有代码正在运行' : run.pending ? '正在请求运行…' : '运行代码'}</button></div>
      <p className="sandbox-run-hint">仅运行这次提交已保存的 Python 正文或 .py 附件；编辑器中未保存的修改不会执行。</p>
      {!validSource && <p className="sandbox-run-hint">本次提交没有可运行的 Python 正文或 .py 附件。</p>}</>
      : <p className="sandbox-run-hint">此提交已不在当前可运行范围内，保留历史运行记录供查看。</p>}
    <ErrorNotice error={run.error} />
    {saved.map(item => <SandboxExecutionCard key={item.id} execution={item} timezone={timezone}
      sourceLabel={item.attachment_id ? submission.attachments?.find(file => file.id === item.attachment_id)?.filename ?? '已保存的 Python 附件' : '已保存的正文'}
      onCancel={runningExecution(item.status) ? () => void stop(item) : undefined} cancelling={cancel.pending}
      error={cancel.error} />)}
  </div>
}
