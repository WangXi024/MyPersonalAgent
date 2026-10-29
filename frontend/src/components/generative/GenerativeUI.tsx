import { useState } from 'react'
import { ExternalLink, Globe2, ListChecks } from 'lucide-react'
import type { ChecklistBlock, GenUIBlock, ResourceListBlock } from '../../api/types'
import { parseBlocks, resourceUrl } from './blocks'
import './generative.css'

interface BlockProps<T extends GenUIBlock> { block: T; timezone?: string }

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
}

function RenderBlock({ block, timezone }: BlockProps<GenUIBlock>) {
  switch (block.type) {
    case 'resource_list': return renderers.resource_list(block, timezone)
    case 'checklist': return renderers.checklist(block)
  }
}

export function GenerativeUI({ blocks, timezone }: { blocks: unknown; timezone?: string }) {
  const parsed = parseBlocks(blocks)
  if (!parsed.length) return null
  return <div className="genui-blocks">{parsed.map((block, index) => block
    ? <RenderBlock key={block.id} block={block} timezone={timezone} />
    : <p className="genui-unavailable" key={`unavailable/${index}`}>这部分内容暂时无法展示，请参考文字回复。</p>)}</div>
}
