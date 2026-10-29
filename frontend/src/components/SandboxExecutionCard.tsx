import { Download, LoaderCircle, Square, Terminal } from 'lucide-react'
import type { SandboxExecution, SandboxExecutionStatus } from '../api/types'
import { ErrorNotice, timeLabel } from './ui'
import './sandbox-execution.css'

export const runningExecution = (status: SandboxExecutionStatus) => ['accepted', 'running', 'cancelling'].includes(status)
const labels: Record<SandboxExecutionStatus, string> = {
  accepted: '等待运行', running: '正在运行', cancelling: '正在取消', succeeded: '运行已结束', failed: '运行失败',
  cancelled: '已取消', superseded: '运行已失效', unavailable: '执行服务暂不可用', expired: '运行已超时',
}
const phases: Record<string, string> = { queued: '排队等待', preparing: '准备环境', creating: '创建隔离环境', running: '执行脚本', returning: '回传结果', finished: '执行阶段已结束' }

function reportedMetric(available: unknown, value: unknown, divisor: number): number | null {
  return available === true && typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value / divisor : null
}

function metricLabel(value: number) {
  return value > 0 && value < 0.001 ? '< 0.001' : value.toLocaleString('zh-CN', { maximumFractionDigits: 3 })
}

export function SandboxExecutionCard({ execution, sourceLabel, timezone, onCancel, cancelling = false, error = null }: {
  execution: SandboxExecution; sourceLabel: string; timezone?: string; onCancel?: () => void; cancelling?: boolean; error?: Error | null
}) {
  const active = runningExecution(execution.status)
  const neverSubmitted = execution.submitted_at === null
    && ['succeeded', 'failed', 'cancelled', 'superseded', 'unavailable', 'expired'].includes(execution.status)
  const usage = execution.resource_usage
  const cpu = reportedMetric(usage?.cpu_usage_available, usage?.cpu_usage_usec, 1e6)
  const memory = reportedMetric(usage?.peak_memory_available, usage?.peak_memory_bytes, 1048576)
  return <section className="sandbox-execution" aria-label={`Python运行记录：${execution.id}`}>
    <header className="row spread wrap"><strong className="row"><Terminal size={15} aria-hidden="true" />Python 运行记录</strong><span className={`sandbox-status sandbox-${execution.status}`}>{active && <LoaderCircle size={12} className="spin" aria-hidden="true" />}{labels[execution.status] ?? '状态更新中'}</span></header>
    <p className="sandbox-source">运行内容：{sourceLabel} · <time dateTime={execution.created_at}>{timeLabel(execution.created_at, timezone)}</time></p>
    {phases[execution.phase] && <p className="sandbox-source">当前阶段：{phases[execution.phase]}</p>}
    <p className="sandbox-evidence-note">此记录展示脚本的实际运行，不判定作业是否通过；程序自行打印的“通过”也不代表验证结果。</p>
    {(cpu !== null || memory !== null) && <div className="sandbox-resource-usage" aria-label="运行资源报告">
      {cpu !== null && <span>累计 CPU {metricLabel(cpu)} 秒</span>}
      {memory !== null && <span>峰值内存 {metricLabel(memory)} MiB</span>}
      {usage?.resource_usage_final === false && <small>执行中报告</small>}
    </div>}
    {execution.error_message && <p className="sandbox-error" role="status">{execution.error_message}</p>}
    {execution.stdout && <details className="sandbox-output" open><summary>程序输出 stdout</summary><pre>{execution.stdout}</pre></details>}
    {execution.stderr && <details className="sandbox-output" open><summary>错误输出 stderr</summary><pre>{execution.stderr}</pre></details>}
    {!active && !execution.stdout && !execution.stderr && <p className="sandbox-empty-output">{neverSubmitted ? '本次未执行代码。' : '本次未保存程序输出。'}</p>}
    {execution.artifacts.length > 0 && <div className="sandbox-artifacts"><h4>运行产物</h4><ul>{execution.artifacts.map(artifact => {
      const expected = `/api/v1/sandbox-executions/${encodeURIComponent(execution.id)}/artifacts/${encodeURIComponent(artifact.id)}`
      return <li key={artifact.id}>{artifact.download_url === expected
        ? <a href={expected} download={artifact.filename}><Download size={13} aria-hidden="true" />{artifact.filename}</a>
        : <span>{artifact.filename}（下载暂不可用）</span>}<small>{artifact.size.toLocaleString('zh-CN')} 字节</small></li>
    })}</ul></div>}
    <footer className="row spread wrap"><span>{neverSubmitted ? '未创建隔离环境' : execution.cleanup_done ? '隔离环境已清理' : '等待隔离环境清理确认'}</span><span>{neverSubmitted ? '无需结果回执' : execution.delivery_done ? '结果回执已确认' : '结果回执待确认'}</span></footer>
    <ErrorNotice error={error} />
    {onCancel && active && <button type="button" className="text-button sandbox-cancel" disabled={cancelling || execution.status === 'cancelling'} onClick={onCancel}><Square size={12} aria-hidden="true" />{cancelling || execution.status === 'cancelling' ? '正在取消…' : '取消运行'}</button>}
  </section>
}
