import { BrowserRouter, Link, Navigate, Route, Routes, useParams } from 'react-router-dom'
import { AppProvider } from './state'
import { Layout } from './components/Layout'
import { Login } from './pages/Login'
import { Today } from './pages/Today'
import { Goals } from './pages/Goals'
import { NewGoal } from './pages/NewGoal'
import { GoalDetailPage } from './pages/GoalDetail'
import { Learn } from './pages/Learn'
import { Assistant } from './pages/Assistant'
import { Settings } from './pages/Settings'

function LearnRoute() { const { goalId } = useParams(); return <Learn key={goalId} /> }

export function App() {
  return <BrowserRouter><AppProvider><Routes><Route path="/login" element={<Login />} /><Route element={<Layout />}><Route index element={<Navigate to="/today" replace />} /><Route path="/today" element={<Today />} /><Route path="/assistant" element={<Assistant />} /><Route path="/goals" element={<Goals />} /><Route path="/goals/new" element={<NewGoal />} /><Route path="/goals/:goalId" element={<GoalDetailPage />} /><Route path="/goals/:goalId/learn" element={<LearnRoute />} /><Route path="/settings" element={<Settings />} /><Route path="*" element={<div className="empty-state"><h1>这里还没有内容</h1><p>你可以回到今天，继续学习。</p><Link className="button primary" to="/today">回到今天</Link></div>} /></Route></Routes></AppProvider></BrowserRouter>
}
