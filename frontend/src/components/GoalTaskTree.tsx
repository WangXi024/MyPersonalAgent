import { ChevronRight } from 'lucide-react'
import type { Goal, Milestone, Task } from '../api/types'
import { EmptyState, Status, TaskRow } from './ui'

export function GoalTaskTree({ goal, milestones, tasks, planVersion }: { goal: Goal; milestones: Milestone[]; tasks: Task[]; planVersion?: number }) {
  if (!goal.active_plan_id) return <div className="card"><EmptyState title="任务树将在计划确认后出现">确认计划后，这里会按里程碑展示任务和实际进度。</EmptyState></div>
  const milestoneIds = new Set(milestones.map(milestone => milestone.id))
  const groups = milestones.map(milestone => ({ ...milestone, tasks: tasks.filter(task => task.milestone_id === milestone.id) }))
  const ungrouped = tasks.filter(task => !task.milestone_id || !milestoneIds.has(task.milestone_id))
  if (ungrouped.length) groups.push({ id: 'ungrouped', plan_id: goal.active_plan_id, title: '未归入里程碑的任务', criteria: [], status: '', position: milestones.length, tasks: ungrouped })
  return <div className="card goal-task-tree">
    <div className="goal-tree-root">
      <div className="row spread wrap"><h3>{goal.title}</h3><Status value={goal.status} /></div>
      {planVersion !== undefined && <small>当前计划 v{planVersion}</small>}
      <p className="task-progress-note">当前计划已完成 {tasks.filter(task => task.status === 'completed').length} / {tasks.length} 项任务；跳过不计为完成。</p>
    </div>
    {groups.length ? <div className="goal-tree-branches">{groups.map(group => <details className="goal-tree-branch" key={group.id} open>
      <summary>
        <ChevronRight className="goal-tree-chevron" size={16} aria-hidden="true" />
        <h4>{group.title}</h4>
        <span className="goal-tree-summary-meta">{group.status && <Status value={group.status} />}<span>已完成 {group.tasks.filter(task => task.status === 'completed').length} / {group.tasks.length}</span></span>
      </summary>
      <div className="goal-tree-content">
        {group.criteria.length > 0 && <ul className="criteria-list">{group.criteria.map((criterion, index) => <li key={index}>{criterion}</li>)}</ul>}
        {group.tasks.length ? <div className="goal-tree-tasks">{group.tasks.map(task => <TaskRow key={task.id} task={task} />)}</div> : <p className="goal-tree-empty">此里程碑暂无当前任务</p>}
      </div>
    </details>)}</div> : <p className="goal-tree-empty">当前计划暂无正式任务。</p>}
  </div>
}
