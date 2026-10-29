import { ChartNoAxesColumnIncreasing } from 'lucide-react'
import type { ProgressBlock } from '../../api/types'
import { statusLabels, timeLabel } from '../ui'

export function ProgressCard({ block, timezone }: { block: ProgressBlock; timezone?: string }) {
  const value = block.snapshot
  const counts: [string, number][] = [['任务总数', value.tasks.total], ['已完成', value.tasks.completed], ['学习中', value.tasks.in_progress], ['等待评估', value.tasks.awaiting_review], ['需要巩固', value.tasks.needs_revision], ['待开始', value.tasks.ready], ['已跳过', value.tasks.skipped]]
  return <section className="genui-card genui-progress" aria-label={`学习进度快照：${block.title}`}>
    <header className="genui-heading"><ChartNoAxesColumnIncreasing size={16} aria-hidden="true" /><h3>{block.title}</h3></header>
    <p className="genui-progress-goal">{value.title}<span>{statusLabels[value.goal_status]}</span></p>
    <dl className="genui-progress-counts">{counts.map(([label, count]) => <div key={label}><dt>{label}</dt><dd>{count}</dd></div>)}</dl>
    <div className="genui-progress-evidence"><p>知识记录 {value.skills.total} 项，其中待复习 {value.skills.needs_review} 项。</p><p>当前提交的有效 AI 审阅 {value.evidence.ai_review} 条；已验证测试证据 {value.evidence.verified_tests} 条。</p></div>
    <p className="genui-footnote"><time dateTime={value.as_of}>截至 {timeLabel(value.as_of, timezone)}</time>的记录快照，任务统计来自当时的当前计划，包含已跳过任务。AI 审阅条数和任务完成数不代表知识掌握率。</p>
  </section>
}
