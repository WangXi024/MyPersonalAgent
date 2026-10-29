import { CheckCheck, LoaderCircle, Square } from 'lucide-react'
import type { VerificationCase, VerificationRun } from '../api/verification'
import { ErrorNotice, timeLabel } from './ui'
import './verification.css'

export const verificationActive = (run: VerificationRun) => ['accepted', 'running', 'cancelling'].includes(run.status)
export const verifiedCase = (item: VerificationCase) => item.status === 'passed' && item.exit_code === 0 && item.matched === true && item.output_complete === true && item.output_truncated === false && item.utf8_valid === true && !item.timed_out
const labels: Record<VerificationRun['status'], string> = { accepted: '等待验证', running: '正在验证样例', cancelling: '正在取消', passed: '验证结论尚待确认', failed: '有样例未符合规则', inconclusive: '本次未能得出验证结论', cancelled: '已取消验证', superseded: '本次验证已失效', expired: '验证已超时' }
const cases: Record<VerificationCase['status'], string> = { pending: '等待执行', running: '正在执行', passed: '结果尚待确认', failed: '未符合规则', inconclusive: '结果不完整', cancelled: '已停止' }
export function VerificationRunCard({ run, sourceLabel, contractVersion, timezone, onCancel, cancelling = false, error = null }: {
  run: VerificationRun; sourceLabel: string; contractVersion?: number; timezone?: string; onCancel?: () => void; cancelling?: boolean; error?: Error | null
}) {
  const active = verificationActive(run)
  const passed = run.status === 'passed' && run.tests_verified === true && /^[a-f0-9]{64}$/.test(run.source_sha256) && /^[a-f0-9]{64}$/.test(run.contract_hash) && run.cases.length > 0 && run.cases.every(verifiedCase)
  return <section className="verification-run" aria-label={`程序验证记录：${run.id}`}>
    <header className="row spread wrap"><strong className="row"><CheckCheck size={15} />程序验证记录</strong><span className="row">{active && <LoaderCircle size={13} className="spin" />}{passed ? '已确认样例验证通过' : labels[run.status]}</span></header>
    <p className="verification-note">{sourceLabel}{contractVersion ? ` · 规则 v${contractVersion}` : ''} · 要求 v{run.requirements_version} · {timeLabel(run.created_at, timezone)}</p>
    <p className="verification-note">仅判断已确认的输入输出样例，不自动完成任务或更新知识掌握记录。AI 评论另行展示。</p>
    {run.source_sha256 && <details className="verification-hash"><summary>查看本次代码 SHA-256</summary><code>{run.source_sha256}</code></details>}
    {run.error_message && <p className="verification-error" role="status">{run.error_message}</p>}
    {run.cases.map((item, index) => <details key={item.id} className="verification-case" open={item.status === 'failed' || item.status === 'inconclusive'}><summary>样例 {index + 1} · {item.name}<span>{verifiedCase(item) ? '符合确认规则' : cases[item.status]}</span></summary>
      <p className="verification-note">{item.exit_code === null ? item.cleanup_done ? '本例未返回可信退出码' : '尚未返回退出码' : `实际退出码 ${item.exit_code}`}{item.timed_out ? ' · 运行超时' : ''}{item.output_truncated ? ' · 输出已截断，不能判定通过' : ''}{item.utf8_valid === false ? ' · 输出不是有效 UTF-8 文本' : ''}{item.matched === false ? ' · 实际输出与预期不一致' : ''}{!item.output_complete ? ' · 尚无完整输出' : ''}</p>
      {item.stdout && <div className="verification-output"><strong>实际 stdout</strong><pre>{item.stdout}</pre></div>}{item.stderr && <div className="verification-output"><strong>实际 stderr</strong><pre>{item.stderr}</pre></div>}
      {!item.stdout && !item.stderr && <p className="verification-note">本例尚无可展示的程序输出。</p>}
    </details>)}
    <footer className="verification-note">{run.cleanup_done ? '测试环境清理已确认' : '等待测试环境清理确认'}</footer>
    <ErrorNotice error={error} />
    {onCancel && (active || !run.cleanup_done) && <button type="button" className="text-button" disabled={cancelling || run.status === 'cancelling'} onClick={onCancel}><Square size={12} />{cancelling || run.status === 'cancelling' ? '正在确认停止与清理' : active ? '取消程序验证' : '重试清理'}</button>}
  </section>
}
