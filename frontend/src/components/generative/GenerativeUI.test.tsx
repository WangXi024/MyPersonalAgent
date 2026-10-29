import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { ChecklistBlock, ResourceListBlock } from '../../api/types'
import { GenerativeUI } from './GenerativeUI'
import { parseBlock } from './blocks'

const resources: ResourceListBlock = {
  type: 'resource_list', id: 'reading-1', title: '先阅读官方文档', items: [{
    id: 'source-1', title: 'Python 官方教程', url: 'https://docs.python.org/zh-cn/3/tutorial/',
    snippet: '从变量、控制流程和函数开始。', retrieved_at: '2026-10-29T02:00:00Z',
  }],
}
const checklist: ChecklistBlock = { type: 'checklist', id: 'steps-1', title: '阅读时试一试', items: ['解释函数参数', '写出返回值'] }

describe('controlled Generative UI', () => {
  it('renders retrieved references with visible origins, timestamps and safe external navigation', () => {
    render(<GenerativeUI blocks={[resources]} timezone="Asia/Shanghai" />)
    const card = screen.getByRole('region', { name: '参考资料：先阅读官方文档' })
    const link = within(card).getByRole('link', { name: /Python 官方教程/ })
    expect(link).toHaveAttribute('href', 'https://docs.python.org/zh-cn/3/tutorial/')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(link).toHaveAttribute('referrerpolicy', 'no-referrer')
    expect(within(card).getByText('docs.python.org')).toBeVisible()
    expect(within(card).getByText('检索于 10/29 10:00')).toHaveAttribute('dateTime', '2026-10-29T02:00:00Z')
    expect(within(card).getByText(resources.items[0].snippet)).toBeVisible()
  })

  it('renders model-supplied titles and snippets as text without executing markup or loading images', () => {
    const title = '<img src="https://tracker.example/pixel" onerror="alert(1)">'
    const snippet = '<script>fetch("/api/v1/goals")</script>'
    const { container } = render(<GenerativeUI blocks={[{ ...resources, title, items: [{ ...resources.items[0], title, snippet }] }]} />)
    expect(screen.getAllByText(title)).toHaveLength(2)
    expect(screen.getByText(snippet)).toBeVisible()
    expect(container.querySelector('img,script,iframe')).toBeNull()
  })

  it.each(['javascript:alert(1)', 'data:text/html,example', '/api/v1/goals', 'https://docs.python.org@evil.example/', 'https://user:secret@docs.python.org/', 'https://docs.python.org\n.evil.example/'])('rejects unsafe or ambiguous resource links: %s', url => {
    render(<GenerativeUI blocks={[{ ...resources, items: [{ ...resources.items[0], url }] }, checklist]} />)
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(screen.getByText('这部分内容暂时无法展示，请参考文字回复。')).toBeVisible()
    expect(screen.getByRole('region', { name: '建议清单：阅读时试一试' })).toBeVisible()
  })

  it('keeps suggestion checkmarks local and never turns them into learning or API operations', async () => {
    const request = vi.spyOn(globalThis, 'fetch')
    const user = userEvent.setup()
    const view = render(<GenerativeUI blocks={[checklist]} />)
    const mark = screen.getByRole('checkbox', { name: '解释函数参数' })
    await user.click(mark)
    expect(mark).toBeChecked()
    expect(screen.getByText('勾选仅作临时阅读标记，不计入学习进度。')).toBeVisible()
    expect(request).not.toHaveBeenCalled()
    view.rerender(<GenerativeUI blocks={[structuredClone(checklist)]} />)
    expect(mark).toBeChecked()
    await user.click(mark)
    expect(mark).not.toBeChecked()
  })

  it('falls back on unknown or malformed components without hiding the valid remaining blocks', () => {
    const blocks = [{ type: 'raw_html', id: 'unsafe', html: '<button>确认创建目标</button>' }, null, { ...checklist, items: ['合法', 42] }, resources]
    const { container } = render(<GenerativeUI blocks={blocks} />)
    expect(screen.getAllByText('这部分内容暂时无法展示，请参考文字回复。')).toHaveLength(3)
    expect(screen.getByRole('link', { name: /Python 官方教程/ })).toBeVisible()
    expect(container.querySelector('button')).toBeNull()
  })

  it('bounds oversized payloads and does not render duplicate component or source identities', () => {
    const view = render(<GenerativeUI blocks={Array.from({ length: 100 }, () => resources)} />)
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(screen.getAllByText('这部分内容暂时无法展示，请参考文字回复。')).toHaveLength(1)
    view.rerender(<GenerativeUI blocks={[resources, resources, { ...resources, id: 'duplicate-source', items: [resources.items[0], resources.items[0]] }]} />)
    expect(screen.getAllByRole('link')).toHaveLength(1)
    expect(screen.getAllByText('这部分内容暂时无法展示，请参考文字回复。')).toHaveLength(2)
  })

  it('does not render an empty wrapper for historical messages with no components', () => {
    const { container, rerender } = render(<GenerativeUI blocks={undefined} />)
    expect(container).toBeEmptyDOMElement()
    rerender(<GenerativeUI blocks={[]} />)
    expect(container).toBeEmptyDOMElement()
  })

  it.each([
    { label: 'component title', limit: 160, block: (value: string) => ({ ...checklist, title: value }) },
    { label: 'checklist item', limit: 500, block: (value: string) => ({ ...checklist, items: [value] }) },
    { label: 'resource title', limit: 200, block: (value: string) => ({ ...resources, items: [{ ...resources.items[0], title: value }] }) },
    { label: 'resource snippet', limit: 2000, block: (value: string) => ({ ...resources, items: [{ ...resources.items[0], snippet: value }] }) },
  ])('matches the backend Unicode code point limit for $label', ({ limit, block }) => {
    const boundary = '😀'.repeat(limit)
    expect(boundary.length).toBe(limit * 2)
    expect(parseBlock(block(boundary))).not.toBeNull()
    expect(parseBlock(block(`${boundary}😀`))).toBeNull()
  })

  it('renders a valid emoji title at the backend limit instead of falling back', () => {
    const title = '😀'.repeat(160)
    render(<GenerativeUI blocks={[{ ...checklist, title }]} />)
    expect(screen.getByRole('region', { name: `建议清单：${title}` })).toBeVisible()
    expect(screen.queryByText('这部分内容暂时无法展示，请参考文字回复。')).not.toBeInTheDocument()
  })
})
