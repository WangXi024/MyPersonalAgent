import { Check, Cpu, LoaderCircle, Plus, Save } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { ModelConfig, ModelConfigInput, ModelConfigs, ModelProvider, PresetModel } from '../api/types'
import { ErrorNotice, Loading, PageHeading, SectionHeading, timeLabel } from '../components/ui'
import { useAction } from '../hooks'
import { useApp } from '../state'
import './model-settings.css'

const providers: Record<ModelProvider, { name: string; base: string; hint: string }> = {
  bailian: { name: '阿里云百炼', base: 'https://dashscope.aliyuncs.com/compatible-mode/v1', hint: '填写百炼控制台中的模型 ID' },
  zhipu: { name: '智谱', base: 'https://open.bigmodel.cn/api/paas/v4', hint: '填写智谱标准推理接口的模型 ID' },
  ark: { name: '火山方舟', base: 'https://ark.cn-beijing.volces.com/api/v3', hint: '填写模型 ID 或推理接入点 ID' },
}
const emptyForm: ModelConfigInput = { name: '', provider: 'bailian', model_id: '', api_base: providers.bailian.base, api_key: '', input_cny_per_million: '', output_cny_per_million: '', usd_cny_rate: '7', expected_version: 0 }
const providerName = (provider: string) => providers[provider as ModelProvider]?.name ?? '部署网关'
const price = (value: string | null) => value === null ? '未配置' : `¥${Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 6 })}`

export function ModelSettings() {
  const { data, revision } = useApp()
  const action = useAction()
  const [models, setModels] = useState<ModelConfigs | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [editing, setEditing] = useState<ModelConfig | null>(null)
  const [source, setSource] = useState<PresetModel | null>(null)
  const [form, setForm] = useState<ModelConfigInput | null>(null)
  const [testing, setTesting] = useState<ModelConfig | null>(null)
  const [notice, setNotice] = useState('')
  const canManage = data?.user.can_manage_models
  const load = useCallback(async () => {
    if (!canManage) return
    try { setModels(await api.modelConfigs()); setError(null) }
    catch (err) { setError(err instanceof Error ? err : new Error('模型列表暂不可用')) }
  }, [canManage])
  useEffect(() => { void load() }, [load, revision])

  const openForm = (model?: ModelConfig, preset?: PresetModel) => {
    action.clearError(); setNotice(''); setEditing(model ?? null); setSource(preset ?? null)
    setForm(model ? { name: model.name, provider: model.provider, model_id: model.model_id, api_base: model.api_base, api_key: '', input_cny_per_million: model.input_cny_per_million, output_cny_per_million: model.output_cny_per_million, usd_cny_rate: model.usd_cny_rate, expected_version: model.version } : preset && preset.provider !== 'gateway' ? { ...emptyForm, name: `${preset.name} · 自定义`, provider: preset.provider, model_id: preset.name, api_base: preset.api_base ?? providers[preset.provider].base, input_cny_per_million: preset.input_cny_per_million ?? '', output_cny_per_million: preset.output_cny_per_million ?? '', usd_cny_rate: preset.usd_cny_rate, source_model_id: preset.id } : { ...emptyForm })
  }
  const save = async (event: FormEvent) => {
    event.preventDefault()
    if (!form) return
    const body = { ...form, api_key: form.api_key || undefined }
    const result = await action.run(`save-model/${editing?.id ?? 'new'}/${JSON.stringify(body)}`, key => api.saveModelConfig(editing?.id ?? null, body, key))
    if (result) { setForm(null); setEditing(null); setSource(null); setNotice('配置已保存。通过连接测试并启用后，即可在对话中选择。'); await load() }
  }
  const toggle = async (model: ModelConfig) => {
    const result = await action.run(`model-enabled/${model.id}/${model.version}/${!model.enabled}`, key => api.setModelEnabled(model.id, !model.enabled, model.version, key))
    if (result) { setNotice(result.enabled ? '模型已启用，可在对话中选择。' : '模型已停用；已经提交的请求仍使用原版本完成。'); await load() }
  }
  const test = async () => {
    if (!testing) return
    const model = testing
    const result = await action.run(`model-test/${model.id}/${model.version}`, key => api.testModelConfig(model.id, model.version, key))
    if (result) { setTesting(null); await load() }
  }
  const previous = editing ?? source
  const reuseKey = !!form && !!previous && previous.provider === form.provider && previous.api_base === form.api_base
  if (!canManage) return <><PageHeading title="模型配置" description="模型配置由部署管理员维护。" /><div className="notice">当前账号没有模型管理权限。<Link to="/assistant">返回对话</Link></div></>
  return <div className="model-settings-page">
    <PageHeading title="模型配置" description="管理模型连接，在每段对话中选择适合的模型。" action={<button className="button primary" disabled={action.pending} onClick={() => openForm()}><Plus size={16} />新增模型</button>} />
    <ErrorNotice error={error} retry={() => void load()} /><ErrorNotice error={action.error} />
    {notice && <div className="notice success" role="status"><Check size={16} />{notice}</div>}
    {form && <form className="card model-config-form" onSubmit={save}>
      <SectionHeading title={editing ? '编辑模型' : source ? '复制部署配置' : '新增模型'} aside={<button type="button" className="text-button" disabled={action.pending} onClick={() => { setForm(null); action.clearError() }}>取消</button>} />
      <p className="model-note">{editing ? '保存修改后需重新测试并启用。已提交的请求继续使用原版本。' : source ? '复制为独立配置，原有部署模型继续可用。' : '使用供应商的标准推理接口。保存配置不会调用模型。'}</p>
      <fieldset disabled={action.pending} className="model-fields">
        <label>显示名称<input required maxLength={100} value={form.name} onChange={event => setForm({ ...form, name: event.target.value })} placeholder="例如：日常学习助手" /></label>
        <label>供应商<select value={form.provider} onChange={event => { const provider = event.target.value as ModelProvider; setForm({ ...form, provider, api_base: providers[provider].base, model_id: '', api_key: '' }) }}>{Object.entries(providers).map(([id, provider]) => <option value={id} key={id}>{provider.name}</option>)}</select></label>
        <label className="model-field-wide">模型 ID<input required maxLength={200} value={form.model_id} onChange={event => setForm({ ...form, model_id: event.target.value })} placeholder={providers[form.provider].hint} /></label>
        <label className="model-field-wide">API 地址<input type="url" required maxLength={500} value={form.api_base} onChange={event => setForm({ ...form, api_base: event.target.value })} /><small>填写与密钥地域匹配的官方 HTTPS 地址，不使用 Coding Plan 接口。</small></label>
        <label className="model-field-wide">API Key<input type="password" autoComplete="new-password" required={!reuseKey} minLength={8} maxLength={4096} value={form.api_key ?? ''} onChange={event => setForm({ ...form, api_key: event.target.value })} placeholder={reuseKey ? '留空复用已配置的密钥；填写新值则替换' : '粘贴供应商 API Key'} /><small>密钥加密保存在服务端，保存后不回显，也不会写入浏览器存储。</small></label>
        <label>输入价格（元 / 百万 Token）<input type="number" required min={0} max={1000000} step="0.000001" value={form.input_cny_per_million} onChange={event => setForm({ ...form, input_cny_per_million: event.target.value })} /></label>
        <label>输出价格（元 / 百万 Token）<input type="number" required min={0} max={1000000} step="0.000001" value={form.output_cny_per_million} onChange={event => setForm({ ...form, output_cny_per_million: event.target.value })} /></label>
        <details className="model-field-wide model-budget"><summary>预算换算</summary><label>每美元折合人民币<input type="number" required min="0.000001" max={1000} step="0.000001" value={form.usd_cny_rate} onChange={event => setForm({ ...form, usd_cny_rate: event.target.value })} /></label><p>页面费用用人民币展示。网关预算以美元核算，按此值换算；这是本地估算，实际费用以供应商账单为准。</p></details>
      </fieldset>
      <div className="form-footer"><small>填写该模型适用的当前费率。</small><button className="button primary" disabled={action.pending}>{action.pending ? <LoaderCircle size={16} className="spin" /> : <Save size={16} />}保存配置</button></div>
    </form>}
    {!models && !error && <Loading />}
    {models && <section className="section"><SectionHeading title="可管理模型" aside={<span className="section-note">{models.items.length} 个</span>} />
      <div className="model-list">{models.items.length ? models.items.map(model => <article className="card model-config-card" key={model.id}>
        <div className="row spread model-card-heading"><div className="row"><Cpu size={18} /><h3>{model.name}</h3></div><span className={`model-state ${model.enabled ? 'enabled' : ''}`}>{model.enabled ? '已启用' : '未启用'}</span></div>
        <p className="model-identity">{providerName(model.provider)} · {model.model_id}</p><p className="model-endpoint">{model.api_base}</p>
        <div className="model-facts"><span>密钥已配置</span><span>输入 {price(model.input_cny_per_million)} / 百万 Token</span><span>输出 {price(model.output_cny_per_million)} / 百万 Token</span></div>
        {model.test_result ? <div className={`model-test-result ${model.test_result.status}`}><strong>{model.test_result.message}</strong><small>{timeLabel(model.test_result.at, data?.user.timezone)}{model.test_result.estimated_cost_cny !== undefined && ` · 本次估算 ¥${model.test_result.estimated_cost_cny}`}</small></div> : <p className="model-note">尚未验证连接</p>}
        <div className="model-actions"><button className="button secondary" disabled={action.pending} onClick={() => { action.clearError(); setTesting(model) }}>测试连接</button><button className="text-button" disabled={action.pending} onClick={() => openForm(model)}>编辑</button><button className="text-button" disabled={action.pending || (!model.enabled && model.test_result?.status !== 'passed')} onClick={() => void toggle(model)}>{model.enabled ? '停用' : '启用'}</button></div>
        {testing?.id === model.id && <div className="model-test-confirm"><p>将发送 1 次固定测试请求，最多生成 512 个 Token，会产生少量费用。测试不会发送你的学习对话。</p><div className="row"><button className="button primary" disabled={action.pending} onClick={() => void test()}>{action.pending && <LoaderCircle size={15} className="spin" />}{action.pending ? '正在测试…' : '开始连接测试'}</button><button className="text-button" disabled={action.pending} onClick={() => setTesting(null)}>取消</button></div></div>}
      </article>) : <div className="card model-empty"><Cpu size={24} /><h3>添加一个适合学习的模型</h3><p>保存配置、测试连接并启用后，就能在对话中选择。</p><button className="button secondary" onClick={() => openForm()}>新增模型</button></div>}</div>
    </section>}
    {!!models?.presets.length && <section className="section"><SectionHeading title="部署预置模型" aside={<span className="section-note">由部署配置管理</span>} /><div className="model-list">{models.presets.map(preset => <article className="card model-config-card" key={preset.id}>
      <div className="row spread model-card-heading"><div className="row"><Cpu size={18} /><h3>{preset.name}</h3></div><span className="model-state enabled">可用</span></div><p className="model-identity">{providerName(preset.provider)}</p>
      <div className="model-facts"><span>输入 {price(preset.input_cny_per_million)} / 百万 Token</span><span>输出 {price(preset.output_cny_per_million)} / 百万 Token</span></div>
      <p className="model-note">当前部署模型继续使用。需要在页面中调整时，可复制为独立配置。</p><button className="button secondary" disabled={action.pending || !preset.api_base || preset.provider === 'gateway'} onClick={() => openForm(undefined, preset)}>复制为可编辑配置</button>
    </article>)}</div></section>}
  </div>
}
