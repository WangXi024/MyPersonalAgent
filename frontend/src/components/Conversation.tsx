import { ArrowUp, ArrowUpRight, LoaderCircle, MessageCircle, Sparkles } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { GoalDraft, Message, Operation } from '../api/types'
import { useAction } from '../hooks'
import { useApp } from '../state'
import { ErrorNotice, OperationCard, timeLabel } from './ui'
import { GoalDraftCard } from './GoalDraftCard'

interface Props {
  conversationId: string | null
  goalId?: string
  taskId?: string
  operations: Operation[]
  canSend?: boolean
  activeGoal?: boolean
  compact?: boolean
  workspace?: boolean
  initialMessage?: string
  connectionError?: Error | null
  retryConnect?: () => void
}

export function Conversation({ conversationId, goalId, taskId, operations, canSend = true, activeGoal = true, compact = false, workspace = false, initialMessage = '', connectionError, retryConnect }: Props) {
  const { data, revision, refresh } = useApp()
  const messageAction = useAction()
  const operationAction = useAction()
  const [messages, setMessages] = useState<Message[]>([])
  const [messagesError, setMessagesError] = useState<Error | null>(null)
  const [goalDrafts, setGoalDrafts] = useState<GoalDraft[]>([])
  const [draftsError, setDraftsError] = useState<Error | null>(null)
  const [message, setMessage] = useState(initialMessage)
  const [visibleEnd, setVisibleEnd] = useState(false)
  const chatEnd = useRef<HTMLDivElement>(null)
  const chatPane = useRef<HTMLDivElement>(null)
  const scrollOnLoad = useRef(true)
  const readCursor = useRef(0)
  const loadedMessages = useRef<Message[]>([])
  const wasAtEnd = useRef(true)
  useEffect(() => {
    if (!conversationId) return
    let current = true
    let retry: ReturnType<typeof setTimeout> | undefined
    const load = async () => {
      const collected = [...loadedMessages.current]
      let after = collected.at(-1)?.seq ?? 0
      let hasMore = true
      while (hasMore && current) {
        const page = await api.messages(conversationId, after)
        collected.push(...page.items)
        const next = page.items.at(-1)?.seq ?? after
        hasMore = page.has_more && next > after
        after = next
        readCursor.current = Math.max(readCursor.current, page.read_seq)
      }
      if (current) { loadedMessages.current = collected; setMessages(collected); setMessagesError(null) }
    }
    void load().catch(err => {
      if (!current) return
      setMessagesError(err)
      if (err instanceof ApiError && (err.status === 0 || err.status >= 500)) retry = setTimeout(() => { void refresh() }, 3000)
    })
    return () => { current = false; clearTimeout(retry) }
  }, [conversationId, revision, refresh])
  useEffect(() => {
    if (!conversationId || goalId) return
    let current = true
    let retry: ReturnType<typeof setTimeout> | undefined
    void api.goalDrafts(conversationId).then(result => {
      if (current) { setGoalDrafts(result.items); setDraftsError(null) }
    }).catch(err => {
      if (!current) return
      setDraftsError(err)
      if (err instanceof ApiError && (err.status === 0 || err.status >= 500)) retry = setTimeout(() => { void refresh() }, 3000)
    })
    return () => { current = false; clearTimeout(retry) }
  }, [conversationId, goalId, revision, refresh])
  useEffect(() => {
    if ((scrollOnLoad.current || wasAtEnd.current) && messages.length && chatPane.current) { chatPane.current.scrollTop = chatPane.current.scrollHeight; scrollOnLoad.current = false }
  }, [messages, goalDrafts])
  useEffect(() => {
    if (!chatEnd.current) return
    const observer = new IntersectionObserver(entries => { wasAtEnd.current = entries[0].isIntersecting; setVisibleEnd(entries[0].isIntersecting) }, { threshold: 1 })
    observer.observe(chatEnd.current)
    return () => observer.disconnect()
  }, [conversationId])
  const lastMessageSeq = messages.at(-1)?.seq ?? 0
  useEffect(() => {
    if (!conversationId || !visibleEnd || lastMessageSeq <= readCursor.current) return
    let stopped = false
    const markRead = async () => {
      if (stopped || document.visibilityState !== 'visible') return
      try { await api.command(`/conversations/${conversationId}/read`, { seq: lastMessageSeq }); readCursor.current = Math.max(readCursor.current, lastMessageSeq); await refresh() } catch { /* Read receipts retry on the next visible update. */ }
    }
    void markRead()
    document.addEventListener('visibilitychange', markRead)
    return () => { stopped = true; document.removeEventListener('visibilitychange', markRead) }
  }, [conversationId, visibleEnd, lastMessageSeq, revision, refresh])
  const sendMessage = async (event: FormEvent) => {
    event.preventDefault()
    if (!message.trim() || !conversationId || !canSend) return
    const body = { content: message.trim(), ...(goalId && taskId ? { task_id: taskId } : {}) }
    const result = await messageAction.run(JSON.stringify(body), key => api.command(`/conversations/${conversationId}/messages`, body, key))
    if (result) { setMessage(''); scrollOnLoad.current = true }
  }
  const running = operations.filter(operation => ['accepted', 'running', 'waiting_user', 'failed'].includes(operation.status))
  return <section className={`chat-card card ${compact ? 'chat-compact' : ''}${workspace ? ' chat-workspace' : ''}`} aria-label={goalId ? '学习对话' : '和 Agent 对话'}>
    {!workspace && <div className="chat-header"><div className="row"><span className="chat-avatar"><Sparkles size={19} /></span><div><strong>{goalId ? '和学习伙伴聊聊' : '和 Agent 聊聊'}</strong><small>{goalId ? '主动建议和你的问题，都在这里。' : '目标还没想清楚，也可以直接开始对话。'}</small></div></div>{compact ? <Link className="text-button" to="/assistant">完整对话<ArrowUpRight size={14} /></Link> : <MessageCircle size={18} />}</div>}
    <div className="chat-messages" ref={chatPane}><ErrorNotice error={connectionError ?? null} retry={retryConnect} /><ErrorNotice error={messagesError} retry={() => void refresh()} /><ErrorNotice error={draftsError} retry={() => void refresh()} />{messages.length ? messages.map(item => {
      const linkedGoal = goalId ?? data?.tasks.find(task => task.id === item.task_id)?.goal_id
      const draft = !goalId && item.role === 'assistant' ? goalDrafts.find(proposal => proposal.message_id === item.id) : undefined
      return <article className={`chat-message ${item.role === 'user' ? 'from-user' : 'from-agent'}`} key={item.id}><div className="message-author">{item.role === 'user' ? '你' : '学习伙伴'}{item.type === 'proactive' && <span>主动建议</span>}<time>{timeLabel(item.created_at, data?.user.timezone)}</time></div><div className="message-bubble"><p className="preserve-text">{item.content}</p>{item.task_id && linkedGoal && item.role !== 'user' && <Link className="message-task-link" to={`/goals/${linkedGoal}/learn?task=${item.task_id}`}>查看关联任务 <ArrowUpRight size={13} /></Link>}</div>{draft && <GoalDraftCard key={draft.operation_id} proposal={draft} />}</article>
    }) : <div className="chat-empty">{!workspace && <Sparkles size={28} />}<h3>{workspace ? '今天想学什么？' : goalId ? '不用一个人摸索。' : '想学什么，我们一起聊清楚。'}</h3><p>{workspace ? '说说你的目标，或遇到的问题。' : goalId ? '可以问一个问题，聊聊你卡住的地方，或者告诉我今天有多少时间。' : '说说你的兴趣、困惑，或者今天的学习状态。Agent 可以帮你整理目标草稿，修改并确认后，再一起准备计划。'}</p>{!goalId && <div className="chat-starters"><button type="button" disabled={messageAction.pending} onClick={() => setMessage('我想学点新东西，但还没想好从哪里开始。')}>还没想好学什么</button><button type="button" disabled={messageAction.pending} onClick={() => setMessage('我今天只有 15 分钟，怎样安排学习比较合适？')}>今天只有 15 分钟</button></div>}</div>}{running.map(operation => <div className="chat-operation" key={operation.id}><OperationCard operation={operation} onCancel={() => void operationAction.run(`cancel/${operation.id}`, key => api.command(`/operations/${operation.id}/cancel`, {}, key))} />{operation.status === 'failed' && <button className="text-button retry-button" disabled={operationAction.pending || !canSend || (!activeGoal && operation.kind !== 'tutor')} onClick={() => void operationAction.run(`retry/${operation.id}`, key => api.command(`/operations/${operation.id}/retry`, {}, key))}>重试此操作</button>}</div>)}<ErrorNotice error={operationAction.error} /><div ref={chatEnd} className="chat-end" /></div>
    {!visibleEnd && messages.length > 0 && <button className="latest-message" onClick={() => { if (chatPane.current) chatPane.current.scrollTo({ top: chatPane.current.scrollHeight, behavior: 'smooth' }) }}>查看最新消息 ↓</button>}
    <form className="chat-composer" onSubmit={sendMessage}><ErrorNotice error={messageAction.error} /><label className="sr-only" htmlFor={`chat-input-${goalId ?? 'assistant'}`}>给学习伙伴发消息</label><textarea id={`chat-input-${goalId ?? 'assistant'}`} disabled={!canSend || messageAction.pending} value={message} onChange={event => setMessage(event.target.value)} rows={workspace || compact ? 2 : 3} maxLength={10000} placeholder={canSend ? workspace ? '给 Agent 发消息…' : goalId ? '提个问题，或告诉我你现在的想法…' : '直接和 Agent 说说，你今天想学什么？' : '目标已结束，历史对话仍可查看。'} onKeyDown={event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); event.currentTarget.form?.requestSubmit() } }} /><div className="row spread"><small>{conversationId ? 'Ctrl / ⌘ + Enter 发送' : connectionError ? '对话暂未连接，请重试。' : '正在连接对话…'}</small><button className="send-button" aria-label="发送消息" disabled={messageAction.pending || !message.trim() || !canSend || !conversationId}>{messageAction.pending ? <LoaderCircle className="spin" size={18} /> : <ArrowUp size={20} />}</button></div><p>{workspace ? '对话和学习进度会持续保存。' : '消息会持续保存，关闭页面后也不会丢失。'}</p></form>
  </section>
}
