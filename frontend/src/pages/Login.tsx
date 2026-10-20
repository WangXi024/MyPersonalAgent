import { ArrowRight, LoaderCircle } from 'lucide-react'
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
  return <div className="login-page"><main className="login-form-panel"><form className="login-form" onSubmit={submit}><div className="brand">知行<small>PERSONAL LEARNING</small></div><h1>登录</h1><p>管理学习目标、任务和 Agent 对话。</p><ErrorNotice error={error} /><label>用户名<input required autoComplete="username" value={username} onChange={event => setUsername(event.target.value)} placeholder="输入你的用户名" /></label><label>密码<input required type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} placeholder="输入你的密码" /></label><button className="button primary full-width" disabled={busy}>{busy ? <LoaderCircle size={18} className="spin" /> : <>登录<ArrowRight size={18} /></>}</button><p className="login-help">账号由部署者创建。无法登录时，请联系你的服务管理员。</p></form></main></div>
}
