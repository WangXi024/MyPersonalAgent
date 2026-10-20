import { ArrowRight, Compass, Plus, Sprout } from 'lucide-react'
import { Link } from 'react-router-dom'
import { AssistantChat } from '../components/AssistantChat'
import { PageHeading, SectionHeading, Status } from '../components/ui'
import { useApp } from '../state'

export function Assistant() {
  const { data } = useApp()
  const goals = data?.goals.filter(goal => !['cancelled', 'completed'].includes(goal.status)) ?? []
  return <><PageHeading eyebrow="你的学习伙伴，随时在这里" title="和 Agent 聊聊" description="从一个问题、一点好奇，或者今天的状态开始。无需先创建目标。" /><div className="assistant-page-grid"><AssistantChat /><aside className="assistant-goals"><SectionHeading title="你的学习方向" />{goals.length ? goals.map(goal => <Link className="assistant-goal-link card" to={`/goals/${goal.id}/learn`} key={goal.id}><div className="row spread"><Compass size={17} /><Status value={goal.status} /></div><strong>{goal.title}</strong><span>进入这个目标的学习对话<ArrowRight size={14} /></span></Link>) : <div className="card compact-card"><Sprout size={25} /><h3>先聊一聊，也很好。</h3><p>还没有明确目标时，Agent 可以帮你梳理兴趣、基础和可用时间。</p></div>}<Link className="button secondary full-width" to="/goals/new"><Plus size={16} />创建一个学习目标</Link><p className="assistant-boundary">对话不会自动创建或修改目标。想好方向后，由你确认目标和计划。</p></aside></div></>
}
