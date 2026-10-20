import { AlertCircle, ArrowRight, Check, Circle, Clock3, LoaderCircle, Sparkles } from 'lucide-react'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ApiError } from '../api/client'
import type { Goal, Operation, Task } from '../api/types'

export const statusLabels: Record<string, string> = { draft: '等待计划确认', active: '进行中', paused: '已暂停', completed: '已完成', cancelled: '已取消', ready: '待开始', in_progress: '学习中', awaiting_review: '等待评估', needs_revision: '需要巩固', skipped: '已跳过', accepted: '已接收', running: '处理中', waiting_user: '等待你', succeeded: '已完成', failed: '暂时受阻', superseded: '已过期', proposed: '待确认', rejected: '未采用', suppressed: '已停用', verified: '已验证', learning: '练习中', unverified: '待验证', practicing: '练习中', evidenced: '已有掌握证据', evidence_available: '已有学习证据', needs_review: '需要复习', pending: '待同步' }
export function Status({ value }: { value: string }) { return <span className={`status status-${value}`}>{statusLabels[value] ?? value}</span> }
export function ErrorNotice({ error, retry }: { error: Error | null; retry?: () => void }) {
  if (!error) return null
  return <div className="notice error" role="alert"><AlertCircle size={18} /><div>{error.message}{error instanceof ApiError && error.requestId && <small>请求编号：{error.requestId}</small>}</div>{retry && <button className="text-button" onClick={retry}>重试</button>}</div>
}
export function Loading({ label = '正在读取学习记录' }: { label?: string }) { return <div className="loading" role="status"><LoaderCircle className="spin" size={22} /><span>{label}</span></div> }
export function EmptyState({ title, children, action }: { title: string; children: ReactNode; action?: ReactNode }) { return <div className="empty-state"><span className="empty-symbol"><Sparkles size={30} /></span><h3>{title}</h3><p>{children}</p>{action}</div> }
export function PageHeading({ eyebrow, title, description, action }: { eyebrow?: string; title: string; description?: string; action?: ReactNode }) { return <header className="page-heading"><div>{eyebrow && <div className="eyebrow">{eyebrow}</div>}<h1>{title}</h1>{description && <p>{description}</p>}</div>{action}</header> }
export function SectionHeading({ title, aside }: { title: string; aside?: ReactNode }) { return <div className="section-heading"><h2>{title}</h2>{aside}</div> }
export function TaskRow({ task, goal }: { task: Task; goal?: Goal }) {
  return <Link className="task-row" to={`/goals/${task.goal_id}/learn?task=${task.id}`}><span className={`task-check ${task.status === 'completed' ? 'done' : ''}`}>{task.status === 'completed' ? <Check size={16} /> : <Circle size={18} />}</span><span className="task-copy"><strong>{task.title}</strong><span>{goal?.title ?? task.instructions}</span></span><span className="task-meta"><Status value={task.status} /><small><Clock3 size={12} />{task.estimated_minutes} 分钟</small></span><ArrowRight size={16} /></Link>
}
export function OperationCard({ operation, onCancel }: { operation: Operation; onCancel?: () => void }) {
  const live = ['accepted', 'running'].includes(operation.status)
  const names: Record<string, string> = { plan: '准备学习计划', plan_proposal: '准备学习计划', generate_plan: '准备学习计划', evaluate: '评估学习成果', evaluate_submission: '评估学习成果', evaluation: '评估学习成果', assessment: '评估学习成果', chat: '准备辅导回复', reply: '准备辅导回复', recommend: '准备下一步任务', recommendation: '准备下一步任务', tutor: '准备辅导回复' }
  const stages: Record<string, string> = { queued: '请求已保存，等待处理。', processing: '正在读取目标和学习上下文。', reasoning: '正在生成与当前目标有关的建议。', validating: '正在检查结果和当前计划是否一致。', saved: '结果已保存。', failed: '本次处理未完成，已保存的内容仍然保留。', cancelled: '本次操作已取消。', superseded: '目标或计划已有变化，旧结果不再生效。' }
  return <article className="operation-card"><div className="row spread"><span className="row">{live ? <LoaderCircle size={16} className="spin" /> : <Sparkles size={16} />}<strong>{names[operation.kind] ?? '处理学习请求'}</strong></span><Status value={operation.status} /></div><p>{operation.error_message || (operation.stage && stages[operation.stage]) || (operation.status === 'accepted' ? '请求已保存，等待 Agent 处理。' : operation.status === 'succeeded' ? '结果已保存，可以查看。' : '状态会在结果保存后更新。')}</p>{live && <div className="row spread"><small>可以离开页面，结果会保存在这里。</small>{onCancel && <button className="text-button muted" onClick={onCancel}>取消本次操作</button>}</div>}</article>
}
export function timeLabel(value: string, timezone?: string) { try { return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: timezone }).format(new Date(value)) } catch { return value } }
export function safeMaterialUrl(value: string) { try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : null } catch { return null } }
