import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { MarkdownMessage } from './MarkdownMessage'

describe('Agent Markdown', () => {
  it('renders headings, emphasis, lists, code and GFM tables', () => {
    const { container } = render(<MarkdownMessage content={'## 下一步\n\n先练习 **函数**。\n\n1. 定义参数\n2. 返回结果\n\n```python\ndef add(a, b):\n    return a + b\n```\n\n| 项目 | 状态 |\n| --- | --- |\n| 函数 | 待练习 |'} />)
    expect(screen.getByRole('heading', { name: '下一步', level: 2 })).toBeInTheDocument()
    expect(container.querySelector('strong')).toHaveTextContent('函数')
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
    expect(container.querySelector('pre code')).toHaveTextContent('def add(a, b):')
    expect(screen.getByRole('table')).toHaveTextContent('待练习')
  })

  it('does not execute HTML, dangerous links or automatically load remote images', () => {
    const { container } = render(<MarkdownMessage content={'<script>alert(1)</script>\n\n<img src="x" onerror="alert(1)">\n\n[危险链接](javascript:alert%281%29)\n\n![远程图片](https://example.com/tracker.png)\n\n[文档](https://example.com/docs)'} />)
    expect(container.querySelector('script, img, iframe')).toBeNull()
    expect(screen.getByText('危险链接').closest('a')).toBeNull()
    expect(screen.getByRole('link', { name: '文档' })).toHaveAttribute('rel', 'noopener noreferrer')
    expect(screen.getByRole('link', { name: '文档' })).toHaveAttribute('href', 'https://example.com/docs')
  })
})
