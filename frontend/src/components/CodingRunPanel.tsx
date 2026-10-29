import { useState } from 'react'
import type { FormEvent } from 'react'
import { Code2, LoaderCircle } from 'lucide-react'
import type { CodingRun, CodingRunInput, Goal, Submission, Task } from '../api/types'
import { api } from '../api/client'
import { useAction } from '../hooks'
import { CodingRunCard } from './CodingRunCard'
import { runningExecution } from './SandboxExecutionCard'
import { ErrorNotice } from './ui'

export function CodingRunPanel({ submission, goal, task, runs, timezone }: {
  submission: Submission; goal: Goal; task: Task; runs: CodingRun[]; timezone?: string
}) {
  const start = useAction()
  const cancel = useAction()
  const [opened, setOpened] = useState(false)
  const [instruction, setInstruction] = useState('')
  const [selection, setSelection] = useState('text')
  const [received, setReceived] = useState<CodingRun | null>(null)
  const [cancelTarget, setCancelTarget] = useState<string | null>(null)
  const files = submission.attachments?.filter(file => /\.py$/i.test(file.filename)) ?? []
  const source = selection === 'text' && !submission.text.trim() && files.length ? files[0].id : selection
  const validSource = source === 'text' ? !!submission.text.trim() : files.some(file => file.id === source)
  const current = submission.task_id === task.id && goal.status === 'active' && goal.active_plan_id === task.plan_id
    && task.active_submission_id === submission.id && !['cancelled', 'skipped'].includes(task.status)
    && (submission.requirements_version === undefined || submission.requirements_version === task.requirements_version)
  const records = new Map(runs.filter(run => run.submission_id === submission.id && run.goal_id === goal.id && run.task_id === task.id).map(run => [run.id, run]))
  if (received?.submission_id === submission.id && received.goal_id === goal.id && received.task_id === task.id
    && (!records.has(received.id) || received.updated_at > records.get(received.id)!.updated_at)) records.set(received.id, received)
  const saved = [...records.values()].sort((a, b) => b.created_at.localeCompare(a.created_at))
  const active = saved.some(run => runningExecution(run.status))
  const cleaning = saved.some(run => !run.cleanup_done)
  const busy = active || cleaning
  const launch = async (event: FormEvent) => {
    event.preventDefault()
    if (!current || !validSource || !instruction.trim() || busy) return
    const body: CodingRunInput = { expected_version: task.state_version, expected_goal_revision: goal.control_revision,
      attachment_id: source === 'text' ? null : source, instruction: instruction.trim() }
    const result = await start.run(`${submission.id}/${JSON.stringify(body)}`, key => api.createCodingRun(submission.id, body, key))
    if (result) setReceived(result)
  }
  const stop = async (run: CodingRun) => {
    if (cancel.pending) return
    setCancelTarget(run.id)
    const result = await cancel.run(`cancel/${run.id}`, key => api.cancelCodingRun(run.id, key))
    if (result) setReceived(result)
  }
  return <div className="coding-run-panel">
    {current && <button type="button" className="text-button coding-panel-toggle" aria-expanded={opened} onClick={() => setOpened(value => !value)}><Code2 size={14} aria-hidden="true" />{opened ? '收起编码助手指令' : '编码助手'}</button>}
    {opened && current && <form className="coding-run-form" aria-label="编码助手指令" onSubmit={event => void launch(event)}>
      <p>启动时固定当前学习对话所选模型，基于已保存的 Python 提交进行修改、运行和调试；之后切换对话模型不会改变这次任务。</p>
      {files.length > 0 && <label>代码来源<select aria-label="编码助手代码来源" value={source} disabled={start.pending || busy} onChange={event => setSelection(event.target.value)}>{submission.text.trim() && <option value="text">已保存的正文</option>}{files.map(file => <option key={file.id} value={file.id}>{file.filename}</option>)}</select></label>}
      <label>希望编码助手做什么<textarea rows={3} maxLength={2000} value={instruction} disabled={start.pending || busy} onChange={event => setInstruction(event.target.value)} placeholder="说明需要修改、解释或验证的内容。" /></label>
      <p className="coding-run-boundary">处理过程会使用模型和隔离工作区；生成文件不会自动替你提交作业或改变学习进度。</p>
      {!validSource && <p>这次提交没有可用的 Python 正文或 .py 附件。</p>}
      <button className="button secondary" disabled={!validSource || !instruction.trim() || start.pending || busy}>{start.pending && <LoaderCircle size={14} className="spin" />}{active ? '编码助手正在处理' : cleaning ? '等待工作区清理' : start.pending ? '正在启动…' : '启动编码助手'}</button>
    </form>}
    <ErrorNotice error={start.error} />
    {saved.map(run => <CodingRunCard key={run.id} run={run} timezone={timezone}
      sourceLabel={run.attachment_id ? submission.attachments?.find(file => file.id === run.attachment_id)?.filename ?? '已保存的 Python 附件' : '已保存的正文'}
      onCancel={runningExecution(run.status) ? () => void stop(run) : undefined} cancelling={cancel.pending} error={cancelTarget === run.id ? cancel.error : null} />)}
  </div>
}
