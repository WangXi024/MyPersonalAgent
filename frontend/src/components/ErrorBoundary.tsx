import { Component } from 'react'
import type { ReactNode } from 'react'
import { AlertCircle, RotateCcw } from 'lucide-react'

export class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  render() {
    if (this.state.failed) return <main className="full-screen"><section className="card empty-state" role="alert"><AlertCircle size={30} /><h1>页面遇到了一点问题</h1><p>刷新后会重新读取已保存的学习记录。未发送的输入可能需要重新填写。</p><button className="button primary" onClick={() => window.location.reload()}><RotateCcw size={16} />重新打开工作台</button></section></main>
    return this.props.children
  }
}
