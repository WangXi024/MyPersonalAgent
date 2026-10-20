import { ArrowUpRight, BookOpen, CircleHelp, Compass, LogOut, MessageCircle, Plus, Settings2, Sprout, Sun, WifiOff } from 'lucide-react'
import { NavLink, Outlet, Link, Navigate } from 'react-router-dom'
import { useState } from 'react'
import { useApp } from '../state'
import { ApiError } from '../api/client'
import { ErrorNotice, Loading } from './ui'

export function Layout() {
  const { data, loading, error, connection, syncedAt, refresh, signOut } = useApp()
  const [logoutError, setLogoutError] = useState<Error | null>(null)
  if (loading) return <main className="full-screen"><Loading /></main>
  if (!data && error instanceof ApiError && error.status === 401) return <Navigate to="/login" replace />
  if (!data) return <main className="full-screen"><div className="card"><h1>暂时无法打开工作台</h1><ErrorNotice error={error} retry={() => void refresh()} /></div></main>
  const goals = data.goals.filter(goal => goal.status !== 'cancelled')
  return <div className="app-shell">
    <a className="skip-link" href="#main-content">跳到主要内容</a>
    <aside className="sidebar">
      <Link to="/today" className="brand"><span className="brand-symbol"><Sprout size={25} strokeWidth={1.8} /></span><span>知行<small>PERSONAL LEARNING</small></span></Link>
      <div className="sidebar-caption">我的学习空间</div>
      <nav aria-label="主要导航"><NavLink to="/today"><Sun size={19} />今天<span className="nav-dot" /></NavLink><NavLink to="/assistant"><MessageCircle size={19} />和 Agent 聊聊{data.conversations.some(conversation => conversation.goal_id === null && conversation.unread_count > 0) && <span className="unread-pill">新</span>}</NavLink><NavLink to="/goals"><Compass size={19} />学习目标<span className="nav-count">{goals.length}</span></NavLink></nav>
      <div className="sidebar-divider" />
      <div className="row spread sidebar-caption"><span>正在前进</span><Link to="/goals/new" aria-label="新建目标"><Plus size={16} /></Link></div>
      <div className="sidebar-goals">{goals.slice(0, 5).map(goal => <NavLink key={goal.id} to={`/goals/${goal.id}`}><span className={`goal-dot ${goal.status}`} /><span>{goal.title}</span>{!!goal.unread_count && <span className="unread-pill">{goal.unread_count}</span>}</NavLink>)}{!goals.length && <p>你的第一个目标，从这里开始。</p>}</div>
      <div className="sidebar-note"><BookOpen size={20} /><p>每天一小步，<br />让想学的变成学会的。</p><Link to="/goals/new">开始一个新目标 <ArrowUpRight size={15} /></Link></div>
      <nav className="sidebar-bottom" aria-label="账号导航"><NavLink to="/settings"><Settings2 size={18} />偏好与记忆</NavLink></nav>
      <div className="account"><span className="avatar">{data.user.username.slice(0, 1).toUpperCase()}</span><span><strong>{data.user.username}</strong><small>个人学习空间</small></span><button className="icon-button" aria-label="退出登录" title="退出登录" onClick={() => void signOut().catch(setLogoutError)}><LogOut size={17} /></button></div>
    </aside>
    <div className="main-shell">
      <div className="topbar"><span className="row"><span className={`connection-dot ${connection}`} />{connection === 'connected' ? '学习记录已同步' : connection === 'connecting' ? '正在连接学习空间' : '重新连接中'}{connection === 'reconnecting' && <WifiOff size={14} />}</span><span className="topbar-note"><CircleHelp size={14} /> 站内消息会为你保留</span></div>
      {connection === 'reconnecting' && <div className="connection-banner" role="status">连接暂时中断，页面保留上次的数据。{syncedAt && `最后同步 ${syncedAt.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}`}<button className="text-button" onClick={() => void refresh()}>重新同步</button></div>}
      <main id="main-content" className="main-content"><ErrorNotice error={logoutError || error} retry={() => void refresh()} /><Outlet /></main>
      <footer className="footer"><span>知行 · 把每一步，走扎实。</span><span>你的进度，由真实的学习成果记录。</span></footer>
    </div>
  </div>
}
