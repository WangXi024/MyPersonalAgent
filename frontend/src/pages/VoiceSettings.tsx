import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { LoaderCircle, Mic, Save } from 'lucide-react'
import { api, ApiError } from '../api/client'
import { useAction } from '../hooks'
import { useApp } from '../state'
import { ErrorNotice, Loading, PageHeading } from '../components/ui'
import type { VoiceCapabilities, VoiceConfig, VoiceConfigInput } from '../voice/types'
import { voiceReason } from '../voice/labels'
import './model-settings.css'

export function VoiceSettings() {
  const { data, revision } = useApp()
  const action = useAction()
  const [capabilities, setCapabilities] = useState<VoiceCapabilities | null>(null)
  const [config, setConfig] = useState<VoiceConfig | null>(null)
  const [form, setForm] = useState<VoiceConfigInput | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [saved, setSaved] = useState(false)
  const canManage = data?.user.can_manage_models === true
  const load = useCallback(async () => {
    if (!canManage) return
    try {
      const [value, meta] = await Promise.all([api.voiceConfig(), api.voiceCapabilities()])
      setConfig(value); setCapabilities(meta); setError(null)
      setForm(previous => previous ?? { expected_version: value.version, provider: value.provider, model_id: value.model_id,
        voice: value.voice, endpoint: value.endpoint, api_version: value.api_version, enabled: value.enabled, api_key: '' })
    } catch (error) { setError(error instanceof Error ? error : new Error('语音配置暂不可用。')) }
  }, [canManage])
  useEffect(() => { void load() }, [load, revision])
  const change = (patch: Partial<VoiceConfigInput>) => { setForm(previous => previous ? { ...previous, ...patch } : previous); setSaved(false) }
  const save = async (event: FormEvent) => {
    event.preventDefault()
    if (!form) return
    const body = { ...form, model_id: form.model_id?.trim() || null, voice: form.voice?.trim() || null, api_key: form.api_key || undefined }
    const value = await action.run(`voice-config/${JSON.stringify(body)}`, key => api.saveVoiceConfig(body, key))
    if (value) {
      setConfig(value); setForm({ expected_version: value.version, provider: value.provider, model_id: value.model_id,
        voice: value.voice, endpoint: value.endpoint, api_version: value.api_version, enabled: value.enabled, api_key: '' })
      setSaved(true); await load()
    }
  }
  if (!canManage) return <><PageHeading title="语音配置" description="语音配置由部署管理员维护。" /><div className="notice">当前账号没有语音配置权限。<Link to="/assistant">返回对话</Link></div></>
  return <div className="model-settings-page"><PageHeading title="语音配置" description="准备实时语音连接，供应商可以稍后选择。保存配置不会建立计费会话。" />
    <ErrorNotice error={error} retry={() => void load()} /><ErrorNotice error={action.error} />
    {action.error instanceof ApiError && action.error.status === 409 && <button type="button" className="text-button" onClick={() => { setForm(null); action.clearError(); void load() }}>读取最新配置</button>}
    {saved && <div className="notice success" role="status">语音配置已保存；只有明确开始语音时才建立会话。</div>}
    {!form ? !error && <Loading label="正在读取语音配置" /> : <form className="card model-config-form" onSubmit={save}>
      <div className="row"><Mic size={18} /><h2>实时语音</h2></div>
      <p className="model-note">{capabilities ? voiceReason(capabilities.reason) : '选择供应商、模型并配置密钥后，即可准备启用。'}</p>
      <p className="model-note">语音使用独立的供应商密钥，按单次和每日时长限制额度，不受聊天模型的月度预算控制。目前不提供语音金额估算，实际费用以供应商账单为准。</p>
      <fieldset disabled={action.pending} className="model-fields">
        <label>语音供应商<select value={form.provider ?? ''} onChange={event => change({ provider: event.target.value || null, model_id: null, voice: null, endpoint: null, api_version: null, api_key: '', enabled: false })}><option value="">暂不选择供应商</option>{capabilities?.providers.map(provider => <option key={provider.id} value={provider.id}>{provider.name}{provider.dependency_ready ? '' : '（待部署依赖）'}</option>)}</select></label>
        <label>语音模型 ID<input maxLength={200} disabled={!form.provider} value={form.model_id ?? ''} onChange={event => change({ model_id: event.target.value || null, enabled: false })} placeholder="由所选供应商提供的实时语音模型" /></label>
        <label>音色 ID（选填）<input maxLength={100} disabled={!form.provider} value={form.voice ?? ''} onChange={event => change({ voice: event.target.value || null })} /></label>
        {form.provider === 'azure' && <><label>Azure endpoint<input type="url" value={form.endpoint ?? ''} maxLength={500} onChange={event => change({ endpoint: event.target.value || null, api_key: '', enabled: false })} placeholder="https://资源名.openai.azure.com" /></label><label>API 版本<input value={form.api_version ?? ''} maxLength={80} onChange={event => change({ api_version: event.target.value || null, enabled: false })} /></label></>}
        <label className="model-field-wide">语音 API Key<input aria-label="语音 API Key" type="password" autoComplete="new-password" maxLength={4096} disabled={!form.provider} value={form.api_key ?? ''} onChange={event => change({ api_key: event.target.value })} placeholder={config?.key_configured && config.provider === form.provider && config.endpoint === form.endpoint ? '留空保留服务端密钥；填写新值则替换' : '供应商选定后再填写'} /><small>密钥只写入服务端，不回显，不保存到浏览器存储。</small></label>
        <label className="model-field-wide"><span className="row"><input aria-label="启用实时语音" type="checkbox" disabled={!capabilities?.providers.some(provider => provider.id === form.provider && provider.dependency_ready) || form.provider === 'azure' && (!form.endpoint?.trim() || !form.api_version?.trim()) || !form.provider || !form.model_id?.trim() || !(form.api_key || config?.key_configured && config.provider === form.provider && config.endpoint === form.endpoint)} checked={form.enabled} onChange={event => change({ enabled: event.target.checked })} />启用实时语音</span><small>需要有效配置。对话页面不会自动开始语音或请求麦克风。</small></label>
      </fieldset>
      <p className="model-note">密钥状态：{config?.key_configured ? '服务端已配置' : '尚未配置'}{capabilities && ` · 单次最长 ${capabilities.limits.max_session_seconds} 秒 · 每日 ${capabilities.limits.daily_seconds} 秒`}</p>
      <div className="form-footer"><small>供应商为空时可保存未启用配置。</small><button className="button primary" disabled={action.pending}>{action.pending ? <LoaderCircle size={16} className="spin" /> : <Save size={16} />}保存语音配置</button></div>
    </form>}</div>
}
