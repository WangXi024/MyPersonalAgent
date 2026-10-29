import { Code2, Download, LoaderCircle, Square } from 'lucide-react'
import type { CodingRun, SandboxExecutionStatus } from '../api/types'
import { MarkdownMessage } from './MarkdownMessage'
import { ModelUsage } from './ModelUsage'
import { runningExecution } from './SandboxExecutionCard'
import { ErrorNotice, timeLabel } from './ui'
import './coding-run.css'

const labels: Record<SandboxExecutionStatus, string> = {
  accepted: '等待编码助手', running: '编码助手处理中', cancelling: '正在停止编码助手', succeeded: '编码助手已完成', failed: '编码助手未完成',
  cancelled: '编码助手已取消', superseded: '编码任务已失效', unavailable: '编码服务暂不可用', expired: '编码任务已超时',
}
const stages: Record<string, string> = { queued: '等待调度', opening: '准备工作区', agent: '模型与工具处理中', exporting: '保存生成文件', closing: '销毁工作区', finished: '处理已结束' }

export function CodingRunCard({ run, sourceLabel, timezone, onCancel, cancelling = false, error = null }: {
  run: CodingRun; sourceLabel: string; timezone?: string; onCancel?: () => void; cancelling?: boolean; error?: Error | null
}) {
  const active = runningExecution(run.status)
  const workspace = !run.workspace_created ? run.cleanup_done ? '清理已确认，无需保留工作区' : '尚未确认工作区创建'
    : run.cleanup_done ? '工作区已销毁' : active && run.stage !== 'closing' ? '工作区保留至本次任务结束' : '等待工作区清理确认'
  return <section className="coding-run-card" aria-label={`编码助手记录：${run.id}`}>
    <header className="row spread wrap"><strong className="row"><Code2 size={15} aria-hidden="true" />编码助手</strong><span className="coding-run-status">{active && <LoaderCircle size={12} className="spin" aria-hidden="true" />}{labels[run.status] ?? '状态更新中'}</span></header>
    <p className="coding-run-meta">基于：{sourceLabel} · <time dateTime={run.created_at}>{timeLabel(run.created_at, timezone)}</time></p>
    {(run.usage?.model_name || run.usage?.model_id) && <p className="coding-run-meta">本次固定模型：{run.usage.model_name || run.usage.model_id}</p>}
    <p className="coding-run-instruction preserve-text">你的指令：{run.instruction}</p>
    {stages[run.stage] && <p className="coding-run-meta">当前阶段：{stages[run.stage]}</p>}
    <p className="coding-run-boundary">生成代码供你审阅和下载，之后由你自行提交；助手完成、命令退出码或程序打印内容均不直接判定作业通过。</p>
    {run.error_message && <p className="sandbox-error" role="status">{run.error_message}</p>}
    {run.summary && <div className="coding-run-summary"><MarkdownMessage content={run.summary} /></div>}
    {run.commands.length > 0 && <div className="coding-run-commands"><h4>实际命令记录</h4>{run.commands.map(command => <section key={command.id} className="coding-command" aria-label={`命令记录：${command.id}`}>
      <pre className="coding-command-text">{command.command}</pre>
      <div className="row spread wrap coding-command-meta"><span>{Number.isInteger(command.exit_code) ? `退出码 ${command.exit_code}` : command.status === 'running' ? '命令仍在运行，尚无退出码' : '本次未返回退出码'}</span><time dateTime={command.started_at}>{timeLabel(command.started_at, timezone)}</time></div>
      {command.stdout && <details className="sandbox-output" open><summary>命令输出 stdout</summary><pre>{command.stdout}</pre></details>}
      {command.stderr && <details className="sandbox-output" open><summary>命令错误 stderr</summary><pre>{command.stderr}</pre></details>}
    </section>)}</div>}
    {run.artifacts.length > 0 && <div className="sandbox-artifacts"><h4>生成文件</h4><ul>{run.artifacts.map(file => {
      const url = `/api/v1/coding-runs/${encodeURIComponent(run.id)}/artifacts/${encodeURIComponent(file.id)}`
      return <li key={file.id}>{file.download_url === url ? <a href={url} download={file.filename}><Download size={13} aria-hidden="true" />{file.filename}</a> : <span>{file.filename}（下载暂不可用）</span>}<small>{file.size.toLocaleString('zh-CN')} 字节</small></li>
    })}</ul></div>}
    <ModelUsage usage={run.usage} />
    <p className="coding-workspace-state" role="status">{workspace}</p>
    <ErrorNotice error={error} />
    {active && onCancel && <button type="button" className="text-button sandbox-cancel" onClick={onCancel} disabled={cancelling || run.status === 'cancelling'}><Square size={12} aria-hidden="true" />{cancelling || run.status === 'cancelling' ? '正在停止…' : '停止编码助手'}</button>}
  </section>
}
