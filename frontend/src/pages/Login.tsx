import { ArrowRight, Check, LoaderCircle, Sprout } from 'lucide-react'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { useApp } from '../state'
import { ErrorNotice } from '../components/ui'

export function Login() {
  const { data, refresh } = useApp()
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  if (data) return <Navigate to="/today" replace />
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError(null)
    try { await api.login(username, password); setPassword(''); await refresh(); navigate('/today') }
    catch (err) { setError(err as Error) } finally { setBusy(false) }
  }
  return <div className="login-page"><section className="login-story"><div className="brand"><span className="brand-symbol"><Sprout size={27} /></span><span>知行<small>PERSONAL LEARNING</small></span></div><div className="login-story-content"><div className="eyebrow">给每一个想学的你</div><h1>让好奇心，<br />慢慢长成能力。</h1><p>一个理解你的学习伙伴。<br />一起找到下一步，把每一小步走扎实。</p><div className="learning-path" aria-hidden="true"><span><Check size={17} /></span><i /><span><Sprout size={22} /></span><i /><span className="path-future" /></div></div><small>目标 · 行动 · 有依据的成长</small></section><main className="login-form-panel"><form className="login-form" onSubmit={submit}><div className="eyebrow">欢迎回来</div><h2>继续你的学习旅程</h2><p>登录后，查看任务、消息和你的每一次进步。</p><ErrorNotice error={error} /><label>用户名<input required autoComplete="username" value={username} onChange={event => setUsername(event.target.value)} placeholder="输入你的用户名" /></label><label>密码<input required type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} placeholder="输入你的密码" /></label><button className="button primary full-width" disabled={busy}>{busy ? <LoaderCircle size={18} className="spin" /> : <>进入学习空间<ArrowRight size={18} /></>}</button><p className="login-help">账号由部署者创建。无法登录时，请联系你的服务管理员。</p></form></main></div>
}
