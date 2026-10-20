import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import './markdown.css'

export function MarkdownMessage({ content }: { content: string }) {
  return <div className="markdown-message"><Markdown remarkPlugins={[remarkGfm]} skipHtml components={{
    a: ({ href, children }) => href ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
    img: ({ alt }) => <span>{alt || '图片'}</span>,
    table: ({ children }) => <div className="markdown-table"><table>{children}</table></div>,
  }}>{content}</Markdown></div>
}
