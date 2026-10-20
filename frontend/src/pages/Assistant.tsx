import { ArrowRight, Compass, PanelRight, Plus, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { AssistantChat } from '../components/AssistantChat'
import { OperationCard, SectionHeading, Status } from '../components/ui'
import { useApp } from '../state'

export function Assistant() {
  const { data, connection } = useApp()
  const [progressOpen, setProgressOpen] = useState(false)
  const progressToggle = useRef<HTMLButtonElement>(null)
  const progressClose = useRef<HTMLButtonElement>(null)
  useEffect(() => { if (progressOpen) progressClose.current?.focus() }, [progressOpen])
  const goals = data?.goals.filter(goal => !['cancelled', 'completed'].includes(goal.status)) ?? []
  const operations = data?.operations.filter(operation => ['accepted', 'running', 'waiting_user', 'failed'].includes(operation.status)) ?? []
  const closeProgress = () => { setProgressOpen(false); progressToggle.current?.focus() }
  return <section className={`assistant-workspace${progressOpen ? ' progress-open' : ''}`}>
    <header className="assistant-toolbar"><div className="assistant-toolbar-title"><h1>和 Agent 聊聊</h1><span className={`assistant-sync ${connection}`} role="status">{connection === 'connected' ? '已同步' : connection === 'reconnecting' ? '重连中' : '连接中'}</span></div><button className="text-button assistant-progress-toggle" type="button" ref={progressToggle} aria-expanded={progressOpen} aria-controls="assistant-progress" onClick={() => setProgressOpen(value => !value)}><PanelRight size={17} />学习进度</button></header>
    <div className="assistant-workspace-body">
      <AssistantChat workspace />
      <aside className="assistant-progress" id="assistant-progress" aria-label="学习进度" hidden={!progressOpen} onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); closeProgress() } }}>
        <div className="assistant-progress-heading row spread"><h2>学习进度</h2><button className="icon-button" type="button" ref={progressClose} aria-label="收起学习进度" onClick={closeProgress}><X size={17} /></button></div>
        <div className="assistant-goals"><SectionHeading title="学习目标" />{goals.length ? goals.map(goal => <Link className="assistant-goal-link card" to={`/goals/${goal.id}`} key={goal.id}><div className="row spread"><Compass size={17} /><Status value={goal.status} /></div><strong>{goal.title}</strong><span>查看目标与计划<ArrowRight size={14} /></span></Link>) : <p className="assistant-progress-empty">还没有学习目标，可以在对话中一起整理。</p>}<Link className="text-button" to="/goals/new"><Plus size={15} />创建学习目标</Link></div>
        <section className="assistant-operations"><SectionHeading title="当前操作" />{operations.length ? operations.map(operation => <div className="assistant-operation" key={operation.id}><OperationCard operation={operation} />{operation.goal_id && <Link className="text-button" to={`/goals/${operation.goal_id}`}>{data?.goals.find(goal => goal.id === operation.goal_id)?.title ?? '查看关联目标'}<ArrowRight size={13} /></Link>}</div>) : <p className="assistant-progress-empty">暂时没有待处理的操作。</p>}</section>
      </aside>
    </div>
  </section>
}
