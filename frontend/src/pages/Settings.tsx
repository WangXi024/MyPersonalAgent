import { Check, Clock3, Globe2, LoaderCircle, MessageCircle, Pencil, Save, ShieldCheck, Sparkles, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import type { Memory, Settings as SettingsType } from '../api/types'
import { useAction } from '../hooks'
import { useApp } from '../state'
import { EmptyState, ErrorNotice, Loading, PageHeading, SectionHeading, Status, timeLabel } from '../components/ui'

function MemoryCard({ memory, reload }: { memory: Memory; reload: () => void }) {
  const [editing, setEditing] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [content, setContent] = useState(memory.content)
  const action = useAction()
  const save = async () => { const result = await action.run(`${memory.id}/${content}`, key => api.updateMemory(memory.id, content.trim(), key)); if (result) { setEditing(false); reload() } }
  const remove = async () => { const result = await action.run(`delete/${memory.id}`, key => api.deleteMemory(memory.id, key)); if (result) reload() }
  return <article className="memory-card"><div className="row spread"><span className="memory-source"><Sparkles size={14} />{memory.source_type === 'message' ? '来自学习对话' : memory.source_type === 'evaluation' ? '来自练习反馈' : '学习记忆'}</span><Status value={memory.suppressed ? 'suppressed' : memory.status} /></div>{editing ? <label>修正记忆<textarea rows={3} value={content} onChange={event => setContent(event.target.value)} /></label> : <p className="preserve-text">{memory.content}</p>}<ErrorNotice error={action.error} /><div className="row spread wrap"><small>{timeLabel(memory.created_at)}</small><div className="row">{memory.status === 'failed' && <button className="text-button" disabled={action.pending} onClick={() => void action.run(`retry/${memory.id}`, key => api.command(`/memories/${memory.id}/retry`, {}, key)).then(result => { if (result) reload() })}>重试同步</button>}{!memory.deleted && !memory.suppressed && (editing ? <><button className="text-button" disabled={action.pending || !content.trim()} onClick={() => void save()}><Check size={14} />保存修正</button><button className="text-button muted" onClick={() => { setEditing(false); setContent(memory.content) }}>取消</button></> : <><button className="text-button" onClick={() => setEditing(true)}><Pencil size={13} />修正</button><button className="text-button muted" onClick={() => setDeleting(value => !value)}><Trash2 size={13} />删除</button></>)}</div></div>{deleting && <div className="delete-memory"><p>删除后，这条记忆会立即停止参与推荐。原始聊天仍保留。</p><button className="text-button danger" disabled={action.pending} onClick={() => void remove()}>确认删除记忆</button><button className="text-button muted" onClick={() => setDeleting(false)}>保留</button></div>}</article>
}
export function Settings() {
  const { data, revision } = useApp()
  const [settings, setSettings] = useState<SettingsType | null>(null)
  const [memories, setMemories] = useState<Memory[]>([])
  const [error, setError] = useState<Error | null>(null)
  const [memoryError, setMemoryError] = useState<Error | null>(null)
  const [saved, setSaved] = useState(false)
  const action = useAction()
  const loadMemories = () => { void api.memories().then(result => { setMemories(result.items); setMemoryError(null) }).catch(setMemoryError) }
  useEffect(() => { void api.settings().then(setSettings).catch(setError) }, [])
  useEffect(() => { loadMemories() }, [revision])
  if (!settings) return error ? <ErrorNotice error={error} /> : <Loading label="正在读取你的偏好" />
  const change = <K extends keyof SettingsType>(key: K, value: SettingsType[K]) => { setSettings({ ...settings, [key]: value }); setSaved(false) }
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const body = { timezone: settings.timezone, daily_minutes: settings.daily_minutes, proactive_enabled: settings.proactive_enabled, quiet_start: settings.quiet_start, quiet_end: settings.quiet_end, learning_hour: settings.learning_hour, expected_version: settings.availability_version }
    const result = await action.run(JSON.stringify(body), key => api.updateSettings(body, key))
    if (result) { setSettings(result); setSaved(true) }
  }
  const hours = Array.from({ length: 24 }, (_, index) => index)
  return <><PageHeading title="偏好与记忆" description="设置学习时间和主动消息，查看、修正或删除记忆。" /><div className="settings-grid"><div><form className="card settings-form" onSubmit={submit}><SectionHeading title="学习设置" /><ErrorNotice error={error || action.error} />{action.error instanceof ApiError && action.error.status === 409 && <button type="button" className="text-button" onClick={() => void api.settings().then(value => { setSettings(value); action.clearError(); setSaved(false) }).catch(setError)}>读取最新设置后重新修改</button>}{saved && <div className="notice success" role="status"><Check size={17} />偏好已保存，后续安排会使用新的时间设置。</div>}<div className="setting-section"><h3><Clock3 size={17} />每天留给学习的时间</h3><p>所有目标共享每天的时间预算。</p><label className="inline-input"><input type="number" required min={5} max={720} value={settings.daily_minutes} onChange={event => change('daily_minutes', Number(event.target.value))} />分钟 / 天</label></div><div className="setting-section"><h3><Globe2 size={17} />所在时区</h3><label><span className="sr-only">时区</span><input required list="timezones" value={settings.timezone} onChange={event => change('timezone', event.target.value)} /><datalist id="timezones">{['Asia/Shanghai', 'Asia/Hong_Kong', 'Asia/Tokyo', 'Asia/Singapore', 'Europe/London', 'America/New_York', 'America/Los_Angeles', 'UTC'].map(zone => <option value={zone} key={zone} />)}</datalist></label><small>学习日期与提醒时间以这里为准。</small></div><div className="setting-section"><div className="row spread"><div><h3><MessageCircle size={17} />站内主动消息</h3><p>在对话中收到准备好的任务、复习与跟进建议。</p></div><label className="switch"><input type="checkbox" checked={settings.proactive_enabled} onChange={event => change('proactive_enabled', event.target.checked)} /><span /><span className="sr-only">允许主动跟进</span></label></div><div className="settings-hours"><label>习惯开始学习的时间<select value={settings.learning_hour} onChange={event => change('learning_hour', Number(event.target.value))}>{hours.map(hour => <option key={hour} value={hour}>{String(hour).padStart(2, '0')}:00</option>)}</select></label><label>安静时段开始<select value={settings.quiet_start} onChange={event => change('quiet_start', Number(event.target.value))}>{hours.map(hour => <option key={hour} value={hour}>{String(hour).padStart(2, '0')}:00</option>)}</select></label><label>安静时段结束<select value={settings.quiet_end} onChange={event => change('quiet_end', Number(event.target.value))}>{hours.map(hour => <option key={hour} value={hour}>{String(hour).padStart(2, '0')}:00</option>)}</select></label></div><div className="settings-info">消息会在对话中保留。关闭网页后不会弹出设备通知；再次打开时查看未读消息。</div></div><div className="form-footer"><small>增加时间预算由你主动确认。</small><button className="button primary" disabled={action.pending}>{action.pending ? <LoaderCircle size={16} className="spin" /> : <Save size={16} />}保存偏好</button></div></form><section className="section"><SectionHeading title="学习记忆" aside={<span className="section-note">{memories.length} 条记忆</span>} /><ErrorNotice error={memoryError} retry={loadMemories} /><div className="card">{memories.length ? memories.map(memory => <MemoryCard memory={memory} reload={loadMemories} key={memory.id} />) : <EmptyState title="暂无学习记忆">偏好、常见困难和有效方法可用于后续建议。</EmptyState>}</div></section></div><aside><div className="card privacy-card"><span className="privacy-icon"><ShieldCheck size={26} /></span><h2>权限与记录</h2><p>任务可以调整，目标可以暂停，记忆可以修正或删除。</p><div className="detail-divider" /><h3>记忆与学习记录</h3><p>记忆帮助理解你的偏好。目标状态、作业和评估依据单独保存，不会因为删除记忆而消失。</p><h3>当前账号</h3><p>{data?.user.username}</p><small>模型连接与部署配置由服务管理员管理，浏览器不保存模型密钥。</small></div></aside></div></>
}
