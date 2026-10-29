import { useEffect, useState } from 'react'
import { ExternalLink, Globe2, ListChecks } from 'lucide-react'
import type { ChecklistBlock, GenUIBlock, ProgressBlock, QuizBlock, ResourceListBlock } from '../../api/types'
import { api } from '../../api/client'
import { ErrorNotice } from '../ui'
import { QuizCard } from './QuizCard'
import { ProgressCard } from './ProgressCard'
import { parseBlocks, resourceUrl } from './blocks'
import './generative.css'

interface BlockProps<T extends GenUIBlock> { block: T; timezone?: string; syncing?: boolean }

function ResourceList({ block, timezone }: BlockProps<ResourceListBlock>) {
  const retrievedAt = (value: string) => {
    try { return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: timezone }).format(new Date(value)) }
    catch { return value }
  }
  return <section className="genui-card genui-resources" aria-label={`参考资料：${block.title}`}>
    <header className="genui-heading"><Globe2 size={16} aria-hidden="true" /><h3>{block.title}</h3><span>{block.items.length} 个来源</span></header>
    <ol className="genui-resource-list">{block.items.map((item, index) => {
      const url = resourceUrl(item.url)!
      return <li key={item.id}>
        <div className="genui-resource-meta"><span className="genui-resource-index">{index + 1}</span><span>{url.hostname}</span><time dateTime={item.retrieved_at}>检索于 {retrievedAt(item.retrieved_at)}</time></div>
        <a className="genui-resource-link" href={url.href} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">{item.title}<ExternalLink size={13} aria-hidden="true" /><span className="sr-only">（新窗口打开）</span></a>
        {item.snippet && <p>{item.snippet}</p>}
      </li>
    })}</ol>
    <p className="genui-footnote">来源摘要供参考，打开原文可查看完整内容。</p>
  </section>
}

function Checklist({ block }: BlockProps<ChecklistBlock>) {
  const [marked, setMarked] = useState<ReadonlySet<number>>(new Set())
  return <section className="genui-card genui-checklist" aria-label={`建议清单：${block.title}`}>
    <header className="genui-heading"><ListChecks size={16} aria-hidden="true" /><h3>{block.title}</h3></header>
    <ul>{block.items.map((item, index) => <li key={index}>
      <label className={marked.has(index) ? 'genui-item-marked' : undefined}>
        <input type="checkbox" checked={marked.has(index)} onChange={() => setMarked(previous => {
          const next = new Set(previous)
          if (next.has(index)) next.delete(index); else next.add(index)
          return next
        })} /><span>{item}</span>
      </label>
    </li>)}</ul>
    <p className="genui-footnote">勾选仅作临时阅读标记，不计入学习进度。</p>
  </section>
}

/** Explicit registry: model data selects a known renderer and never introduces executable UI. */
const renderers = {
  resource_list: (block: ResourceListBlock, timezone?: string) => <ResourceList block={block} timezone={timezone} />,
  checklist: (block: ChecklistBlock) => <Checklist block={block} />,
  quiz: (block: QuizBlock, syncing?: boolean) => <QuizCard block={block} syncing={syncing} />,
  progress: (block: ProgressBlock, timezone?: string) => <ProgressCard block={block} timezone={timezone} />,
}

function RenderBlock({ block, timezone, syncing }: BlockProps<GenUIBlock>) {
  switch (block.type) {
    case 'resource_list': return renderers.resource_list(block, timezone)
    case 'checklist': return renderers.checklist(block)
    case 'quiz': return renderers.quiz(block, syncing)
    case 'progress': return renderers.progress(block, timezone)
  }
}

export function GenerativeUI({ blocks, timezone, operationId, revision = 0 }: { blocks: unknown; timezone?: string; operationId?: string | null; revision?: number }) {
  const [synced, setSynced] = useState<{ operationId: string; blocks: unknown } | null>(null)
  const [syncError, setSyncError] = useState<Error | null>(null)
  const [syncing, setSyncing] = useState(false)
  const [retry, setRetry] = useState(0)
  const hasQuiz = parseBlocks(blocks).some(block => block?.type === 'quiz' && block.operation_id === operationId)
  useEffect(() => {
    if (!operationId || !hasQuiz) return
    let current = true
    setSyncing(true)
    void api.operation(operationId).then(value => {
      if (!current) return
      if (value.id !== operationId || value.kind !== 'tutor' || value.status !== 'succeeded' || !Array.isArray(value.result?.ui_components)) throw new Error('练习记录暂时无法同步，请刷新后重试。')
      setSynced({ operationId, blocks: value.result.ui_components }); setSyncError(null)
    }).catch(error => { if (current) setSyncError(error instanceof Error ? error : new Error('练习记录暂时无法同步。')) }).finally(() => { if (current) setSyncing(false) })
    return () => { current = false }
  }, [operationId, revision, hasQuiz, retry])
  const parsed = parseBlocks(synced && synced.operationId === operationId ? synced.blocks : blocks)
  if (!parsed.length) return null
  return <div className="genui-blocks"><ErrorNotice error={syncError} retry={() => setRetry(value => value + 1)} />{parsed.map((block, index) => block && (block.type !== 'quiz' || block.operation_id === operationId)
    ? <RenderBlock key={`${operationId ?? ''}/${block.id}`} block={block} timezone={timezone} syncing={syncing || syncError !== null} />
    : <p className="genui-unavailable" key={`unavailable/${index}`}>这部分内容暂时无法展示，请参考文字回复。</p>)}</div>
}
