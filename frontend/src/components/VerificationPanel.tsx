import { useState } from 'react'
import { Check, LoaderCircle, Plus, ShieldCheck, Trash2 } from 'lucide-react'
import { api } from '../api/client'
import type { Goal, Submission, Task } from '../api/types'
import type { TestCaseInput, TestContract, TestContractInput, VerificationData, VerificationInput, VerificationRun } from '../api/verification'
import { useAction } from '../hooks'
import { ErrorNotice } from './ui'
import { verificationActive, VerificationRunCard } from './VerificationRunCard'
import './verification.css'

export const utf8Bytes = (value: string) => new TextEncoder().encode(value).byteLength
export function VerificationPanel({ submission, task, goal, data, ready, timezone }: {
  submission: Submission; task: Task; goal: Goal; data: VerificationData; ready: boolean; timezone?: string
}) {
  const confirm = useAction(), execute = useAction(), cancel = useAction()
  const [draft, setDraft] = useState<TestCaseInput[]>([{ name: '样例 1', stdin: '', expected_stdout: '' }])
  const [comparator, setComparator] = useState<TestContract['comparator']>('normalize_newlines')
  const [approval, setApproval] = useState<string | null>(null)
  const [editing, setEditing] = useState(false)
  const [confirmed, setConfirmed] = useState<TestContract | null>(null)
  const [received, setReceived] = useState<VerificationRun | null>(null)
  const [selection, setSelection] = useState('text')
  const [contractId, setContractId] = useState('')
  const [cancelId, setCancelId] = useState('')
  const records = new Map(data.contracts.filter(item => item.task_id === task.id && item.goal_id === goal.id).map(item => [item.id, item]))
  if (confirmed) records.set(confirmed.id, confirmed)
  const contracts = [...records.values()].sort((a, b) => b.version - a.version)
  const eligible = contracts.slice(0, 1).filter(item => item.status === 'confirmed' && item.requirements_version === task.requirements_version)
  const contract = eligible.find(item => item.id === contractId) ?? eligible[0]
  const runRecords = new Map(data.runs.filter(item => item.task_id === task.id && item.goal_id === goal.id && item.submission_id === submission.id).map(item => [item.id, item]))
  if (received && (!runRecords.has(received.id) || received.updated_at > runRecords.get(received.id)!.updated_at)) runRecords.set(received.id, received)
  const runs = [...runRecords.values()].sort((a, b) => b.created_at.localeCompare(a.created_at))
  const busy = [...data.runs, ...runs].some(item => verificationActive(item) || !item.cleanup_done)
  const files = submission.attachments?.filter(item => /\.py$/i.test(item.filename)) ?? []
  const source = selection === 'text' && !submission.text.trim() && files.length ? files[0].id : selection
  const validSource = source === 'text' ? !!submission.text.trim() : files.some(item => item.id === source)
  const current = ready && goal.status === 'active' && goal.active_plan_id === task.plan_id && task.active_submission_id === submission.id
    && submission.requirements_version === task.requirements_version && !['cancelled', 'skipped'].includes(task.status)
  const validCases = draft.length >= 1 && draft.length <= 6 && draft.every(item => item.name.trim() && Array.from(item.name.trim()).length <= 100 && utf8Bytes(item.stdin) <= 16384 && utf8Bytes(item.expected_stdout) <= 16384)
  const signature = JSON.stringify({ task: task.id, goal: goal.control_revision, state: task.state_version, requirements: task.requirements_version, comparator, draft })
  const approved = approval === signature
  const update = (index: number, patch: Partial<TestCaseInput>) => setDraft(previous => previous.map((item, position) => position === index ? { ...item, ...patch } : item))
  const confirmRules = async () => {
    if (!current || !approved || !validCases) return
    const body: TestContractInput = { expected_version: task.state_version, expected_goal_revision: goal.control_revision, requirements_version: task.requirements_version,
      confirm: true, comparator, cases: draft.map(item => ({ ...item, name: item.name.trim() })) }
    const value = await confirm.run(JSON.stringify({ task: task.id, requirements: body.requirements_version, comparator: body.comparator, cases: body.cases }), key => api.confirmTestContract(task.id, body, key))
    if (value) { setConfirmed(value); setContractId(value.id); setEditing(false); setApproval(null) }
  }
  const run = async () => {
    if (!current || !contract || !validSource || busy) return
    const body: VerificationInput = { expected_version: task.state_version, expected_goal_revision: goal.control_revision, contract_id: contract.id,
      expected_contract_hash: contract.contract_hash, attachment_id: source === 'text' ? null : source }
    const value = await execute.run(JSON.stringify({ submission: submission.id, contract: contract.id, hash: contract.contract_hash, source }), key => api.createVerification(submission.id, body, key))
    if (value) setReceived(value)
  }
  const stop = async (item: VerificationRun) => { setCancelId(item.id); const value = await cancel.run(`verification-cancel/${item.id}`, key => api.cancelVerification(item.id, key)); if (value) setReceived(value) }
  return <section className="verification-panel" aria-label="已保存提交的程序验证">
    <h3 className="row"><ShieldCheck size={16} />按确认样例验证程序</h3>
    <p className="verification-note">先核对并确认输入与预期输出，再单独运行已保存的 Python 代码。规则确认后不可修改；修改需另存新版本。</p>
    {current ? <>
      {eligible.length > 0 && <label className="verification-selector">确认规则<select aria-label="选择已确认测试规则" value={contract?.id ?? ''} disabled={execute.pending || confirm.pending} onChange={event => setContractId(event.target.value)}>{eligible.map(item => <option key={item.id} value={item.id}>规则 v{item.version} · 要求 v{item.requirements_version}</option>)}</select></label>}
      {eligible.length > 0 && <button type="button" className="text-button" disabled={confirm.pending} onClick={() => { setEditing(value => !value); setApproval(null) }}>{editing ? '收起规则编辑' : '编辑并确认新版本'}</button>}
      {(editing || eligible.length === 0) && <div className="verification-editor">
        <p className="verification-note">绑定任务要求 v{task.requirements_version}。最多 6 个样例，输入与预期输出各限 16 KiB；当前编辑器最多 4096 字符。预期输出留在应用中比较，不交给被测程序。</p>
        <fieldset disabled={confirm.pending}><label>输出比较方式<select value={comparator} onChange={event => setComparator(event.target.value as TestContract['comparator'])}><option value="normalize_newlines">统一换行后严格比较</option><option value="exact">严格比较原始输出</option></select></label><p className="verification-note">{comparator === 'normalize_newlines' ? '仅将 CRLF / CR 统一为 LF，不去除空白或末尾换行。' : '输出必须完全一致，包括空白和末尾换行。'}</p>
          {draft.map((item, index) => <div className="verification-case-editor" key={index}><div className="row spread"><label>样例名称<input value={item.name} maxLength={100} onChange={event => update(index, { name: event.target.value })} /></label><button type="button" className="icon-button" aria-label={`删除样例 ${index + 1}`} disabled={draft.length === 1} onClick={() => setDraft(previous => previous.filter((_, position) => position !== index))}><Trash2 size={14} /></button></div>
            <label>标准输入 stdin<textarea aria-label="标准输入 stdin" value={item.stdin} maxLength={4096} rows={2} onChange={event => update(index, { stdin: event.target.value })} /><small>{utf8Bytes(item.stdin)} 字节</small></label><label>预期标准输出 stdout<textarea aria-label="预期标准输出 stdout" value={item.expected_stdout} maxLength={4096} rows={2} onChange={event => update(index, { expected_stdout: event.target.value })} /><small>{utf8Bytes(item.expected_stdout)} 字节；空白和末尾换行也参与比较</small></label></div>)}
          <button type="button" className="text-button" disabled={draft.length >= 6} onClick={() => setDraft(previous => [...previous, { name: `样例 ${previous.length + 1}`, stdin: '', expected_stdout: '' }])}><Plus size={14} />添加样例</button>
          <label className="verification-approval"><input type="checkbox" checked={approved} onChange={event => setApproval(event.target.checked ? signature : null)} /><span>我已核对全部输入和预期输出，确认用作这项任务的测试规则。</span></label>
          <button type="button" className="button secondary" disabled={!approved || !validCases || confirm.pending} onClick={() => void confirmRules()}>{confirm.pending ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />}确认测试规则</button>
        </fieldset>
      </div>}
      <ErrorNotice error={confirm.error} />
      {contract && <><details className="verification-contract"><summary>查看已确认规则 v{contract.version} · 要求 v{contract.requirements_version}</summary><p className="verification-note">{contract.comparator === 'normalize_newlines' ? '统一换行后严格比较' : '严格比较原始输出'}</p>{contract.cases.map((item, index) => <div key={index}><strong>{item.name}</strong><span>stdin</span><pre>{item.stdin || '（空输入）'}</pre><span>预期 stdout</span><pre>{item.expected_stdout || '（空输出）'}</pre></div>)}<p className="verification-note">要求哈希 <code>{contract.requirements_hash}</code></p><p className="verification-note">规则哈希 <code>{contract.contract_hash}</code></p></details>
        <div className="row wrap verification-run-controls">{files.length > 0 && <label>验证内容<select aria-label="选择已保存的待验证Python代码" disabled={execute.pending || busy} value={source} onChange={event => setSelection(event.target.value)}>{submission.text.trim() && <option value="text">已保存的正文</option>}{files.map(file => <option key={file.id} value={file.id}>{file.filename}</option>)}</select></label>}<button type="button" className="button secondary" disabled={!validSource || execute.pending || busy} onClick={() => void run()}>{execute.pending && <LoaderCircle size={14} className="spin" />}{busy ? '等待已有验证及清理结束' : '按确认规则验证代码'}</button></div><p className="verification-note">代码 SHA-256 由服务端冻结并记录。编辑器里尚未保存的修改不会执行；每例使用独立环境。</p></>}
      <ErrorNotice error={execute.error} />
    </> : <p className="verification-note">{ready ? '此提交已不在当前验证范围内，历史规则与结果保留供查看。' : '正在读取程序验证记录。'}</p>}
    {contracts.filter(item => !current || item.id !== contract?.id).map(item => <details className="verification-contract" key={item.id}><summary>历史确认规则 v{item.version} · 要求 v{item.requirements_version}</summary>{item.cases.map((value, index) => <div key={index}><strong>{value.name}</strong><span>stdin</span><pre>{value.stdin || '（空输入）'}</pre><span>预期 stdout</span><pre>{value.expected_stdout || '（空输出）'}</pre></div>)}<p className="verification-note">规则哈希 <code>{item.contract_hash}</code></p></details>)}
    {runs.map(item => <VerificationRunCard key={item.id} run={item} timezone={timezone} contractVersion={contracts.find(contract => contract.id === item.contract_id)?.version}
      sourceLabel={item.attachment_id ? submission.attachments?.find(file => file.id === item.attachment_id)?.filename ?? '已保存的 Python 附件' : '已保存的正文'}
      onCancel={verificationActive(item) || !item.cleanup_done ? () => void stop(item) : undefined} cancelling={cancel.pending && cancelId === item.id} error={cancelId === item.id ? cancel.error : null} />)}
  </section>
}
