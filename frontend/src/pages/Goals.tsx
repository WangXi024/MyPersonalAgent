import { ArrowRight, Compass, Plus } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useApp } from '../state'
import { EmptyState, PageHeading, Status } from '../components/ui'

export function Goals() {
  const { data } = useApp()
  if (!data) return null
  return <><PageHeading title="学习目标" description="查看学习计划与进度。所有目标共享每天的时间预算。" action={<Link className="button primary" to="/goals/new"><Plus size={17} />新建目标</Link>} />{data.goals.length ? <div className="goal-grid">{data.goals.map(goal => {
    return <Link className="goal-card card" to={`/goals/${goal.id}`} key={goal.id}><div className="row spread"><span className="goal-icon"><Compass size={23} /></span><Status value={goal.status} /></div><h2>{goal.title}</h2><p>{goal.outcome}</p><div className="goal-card-bottom"><span>查看学习路线、任务与成果证据</span><ArrowRight size={18} /></div></Link>
  })}</div> : <div className="card"><EmptyState title="还没有学习目标" action={<Link to="/goals/new" className="button primary">创建学习目标<ArrowRight size={16} /></Link>}>创建目标，说明你希望学会什么或完成什么作品。</EmptyState></div>}</>
}
