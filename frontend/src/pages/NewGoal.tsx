import { ArrowLeft, ArrowRight, Check, LoaderCircle, Sparkles } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { api } from '../api/client'
import { useAction } from '../hooks'
import { useApp } from '../state'
import { ErrorNotice, PageHeading } from '../components/ui'

export function NewGoal() {
  const navigate = useNavigate()
  const { data } = useApp()
  const { pending, error, run } = useAction()
  const [form, setForm] = useState({ title: '', background: '', outcome: '', criteria: '', deadline: '' })
  const change = (field: keyof typeof form, value: string) => setForm(previous => ({ ...previous, [field]: value }))
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const body = { title: form.title.trim(), background: form.background.trim(), outcome: form.outcome.trim(), criteria: form.criteria.split('\n').map(value => value.trim()).filter(Boolean), ...(form.deadline ? { deadline: form.deadline } : {}) }
    const result = await run(JSON.stringify(body), key => api.createGoal(body, key))
    if (result?.resource_id) navigate(`/goals/${result.resource_id}`)
  }
  return <><Link to="/goals" className="back-link"><ArrowLeft size={15} />全部目标</Link><PageHeading eyebrow="从一个具体的期待开始" title="你想学会什么？" description="不必先想清楚所有步骤，告诉我方向，我们一起拆解。" /><div className="form-layout"><form className="card goal-form" onSubmit={submit}><ErrorNotice error={error} /><label>学习目标<span className="required">*</span><input disabled={pending} required maxLength={200} placeholder="例如：学习 Python，做一个自己的记账工具" value={form.title} onChange={event => change('title', event.target.value)} /></label><label>你现在的基础<span className="required">*</span><textarea disabled={pending} required rows={3} maxLength={5000} placeholder="接触过什么？哪些地方还不熟悉？完全零基础也没关系。" value={form.background} onChange={event => change('background', event.target.value)} /></label><label>学完后，希望自己能做到什么？<span className="required">*</span><textarea disabled={pending} required rows={3} maxLength={5000} placeholder="例如：独立编写一个能记录、分类并汇总支出的小工具。" value={form.outcome} onChange={event => change('outcome', event.target.value)} /></label><label>你心中的完成标准<small>选填，每行一项；Agent 会帮助你完善。</small><textarea disabled={pending} rows={3} maxLength={5000} placeholder="能独立完成核心功能\n能解释自己的实现思路" value={form.criteria} onChange={event => change('criteria', event.target.value)} /></label><label>希望完成的日期<small>选填，可以先不设定期限。</small><input disabled={pending} type="date" value={form.deadline} onChange={event => change('deadline', event.target.value)} /></label><div className="form-budget"><span>当前每天可投入 <strong>{data?.user.daily_minutes ?? '—'} 分钟</strong>，多个目标共同使用。</span><Link to="/settings">调整时间</Link></div><div className="form-footer"><small>提交后先准备计划，确认后才开始推进。</small><button className="button primary" disabled={pending}>{pending ? <LoaderCircle className="spin" size={17} /> : <Sparkles size={17} />}{pending ? '正在保存目标' : '一起制定计划'}<ArrowRight size={16} /></button></div></form><aside className="creation-guide"><div className="guide-mark"><Sparkles size={28} /></div><h2>让目标，变得可开始。</h2><p>好的计划会适应你的基础、时间和每一次练习结果。</p><ol><li><span>01</span><div><strong>先理解你的方向</strong><p>明确基础与想实现的成果。</p></div></li><li><span>02</span><div><strong>拆成看得见的阶段</strong><p>给出里程碑、任务与完成依据。</p></div></li><li><span>03</span><div><strong>由你确认，再开始</strong><p>每一次重要改变都由你决定。</p></div></li></ol><div className="row guide-foot"><Check size={16} />先走好一步，不用一下做到全部。</div></aside></div></>
}
