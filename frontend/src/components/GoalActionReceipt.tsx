import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowRight } from 'lucide-react'
import { api } from '../api/client'
import type { GoalActionResult, Operation } from '../api/types'
import { useApp } from '../state'
import { ErrorNotice, OperationCard } from './ui'

export function GoalActionReceipt({ result }: { result: GoalActionResult }) {
  const { data, revision, refresh } = useApp()
  const [loaded, setLoaded] = useState<Operation | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const snapshot = data?.operations.find(item => item.id === result.operation_id)
  useEffect(() => {
    if (!result.operation_id || snapshot) return
    let current = true
    void api.operation(result.operation_id).then(value => {
      if (current) { setLoaded(value); setError(null) }
    }).catch(cause => { if (current) setError(cause) })
    return () => { current = false }
  }, [result.operation_id, snapshot, revision])
  const operation = snapshot ?? (loaded?.id === result.operation_id ? loaded : null)
  return <section className="goal-draft-card" aria-label={`目标操作结果：${result.goal_title || '未执行'}`}>
    <div className="row spread"><strong>{result.goal_title || '操作未执行'}</strong><span className={`status ${result.status === 'applied' ? 'status-succeeded' : 'status-failed'}`}>{result.status === 'applied' ? '要求已处理' : '未执行'}</span></div>
    {result.changes?.deadline && <p>期望完成：{result.changes.deadline}</p>}
    {result.changes?.background && <details><summary>查看已保存的学习要求</summary><p className="preserve-text">{result.changes.background}</p></details>}
    {operation && <OperationCard operation={operation} />}
    {result.operation_id && !operation && !error && <p className="muted">正在读取计划进度…</p>}
    <ErrorNotice error={snapshot ? null : error} retry={() => void refresh()} />
    {result.goal_id && <Link className="button secondary" to={`/goals/${result.goal_id}`}>{result.kind === 'cancel_goal' ? '查看目标历史' : '查看目标与计划'}<ArrowRight size={14} /></Link>}
  </section>
}
