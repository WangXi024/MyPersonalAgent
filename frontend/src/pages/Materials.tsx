import { useCallback, useEffect, useRef, useState } from 'react'
import { Copy, Download, FileText, LoaderCircle, MessageCircle, Square, Upload } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { api, newRequestKey } from '../api/client'
import type { Material, MaterialCapabilities, MaterialPreview } from '../api/materials'
import { useAction } from '../hooks'
import { useApp } from '../state'
import { materialDraft } from '../material-draft'
import { EmptyState, ErrorNotice, Loading, PageHeading, timeLabel } from '../components/ui'
import './materials.css'

const statuses: Record<Material['status'], string> = { accepted: '等待解析', running: '正在解析', cancelling: '正在取消', succeeded: '已提取文字', failed: '本次未能提取文字', cancelled: '已取消解析', unavailable: '解析服务暂不可用' }
const phases: Record<Material['phase'], string> = { queued: '排队等待', preparing: '准备环境', creating: '创建解析环境', running: '正在读取文档', returning: '回传提取结果', finished: '解析阶段已结束' }
export const materialNeedsPolling = (item: Material) => item.status !== 'unavailable' && (item.can_cancel || !item.cleanup_done || !item.delivery_done)
const needsSlot = (item: Material) => ['accepted', 'running', 'cancelling'].includes(item.status) || !item.cleanup_done

function MaterialCard({ material, timezone, onCancel, cancelling, cancelError }: { material: Material; timezone?: string; onCancel: () => void; cancelling: boolean; cancelError: Error | null }) {
  const navigate = useNavigate()
  const [preview, setPreview] = useState<MaterialPreview | null>(null)
  const [excerpt, setExcerpt] = useState('')
  const [excerptLimited, setExcerptLimited] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const [notice, setNotice] = useState('')
  const mounted = useRef(false)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const showPreview = async () => {
    if (pending) return
    setPending(true); setError(null)
    try {
      const value = await api.materialPreview(material.id)
      if (value.material_id !== material.id || value.untrusted !== true || typeof value.text !== 'string' || value.text.length > 64000 || Array.from(value.text).length > 32000 || !['pdf', 'docx'].includes(value.type)) throw new Error('资料预览格式不正确，暂时无法显示。')
      if (mounted.current) { const draft = materialDraft(material.filename, value.text); setPreview(value); setExcerpt(draft.excerpt); setExcerptLimited(draft.truncated); setNotice('') }
    } catch (error) { if (mounted.current) setError(error instanceof Error ? error : new Error('资料预览暂不可用。')) }
    finally { if (mounted.current) setPending(false) }
  }
  const copy = async () => {
    if (!preview) return
    try { if (!navigator.clipboard?.writeText) throw new Error('当前浏览器不支持自动复制，请在下方文本框中选中文字后手动复制。'); await navigator.clipboard.writeText(preview.text); if (mounted.current) setNotice('预览文字已复制；请检查后再粘贴到消息框发送。') }
    catch (error) { if (mounted.current) setError(error instanceof Error ? error : new Error('复制未完成，请手动选择文字复制。')) }
  }
  const extraction = material.extraction
  const successful = material.status === 'succeeded' && material.result_saved && extraction && !extraction.no_text
  return <article className="card material-card" aria-label={`资料解析：${material.filename}`}>
    <header className="row spread wrap"><h2 className="row"><FileText size={18} />{material.filename}</h2><span className="row">{['accepted', 'running', 'cancelling'].includes(material.status) && <LoaderCircle size={13} className="spin" />}{statuses[material.status]}</span></header>
    <p className="material-meta">{material.size.toLocaleString('zh-CN')} 字节 · {timeLabel(material.created_at, timezone)} · {phases[material.phase]}</p>
    {material.error_message && <p className="material-error" role="status">{material.error_message}</p>}
    {extraction?.no_text && <p className="material-note">这份文件没有可提取的文字，可能是空文件或扫描件。当前不提供 OCR，未生成可供学习的文字内容。</p>}
    {successful && <><p className="material-note">提取文字 {extraction.char_count.toLocaleString('zh-CN')} 字符{extraction.page_count === null ? '' : ` · ${extraction.page_count} 页`}{extraction.preview_truncated ? ' · 预览已截断，完整结果可下载' : ''}</p>
      <div className="row wrap material-actions"><button type="button" className="text-button" disabled={pending} onClick={() => void showPreview()}>{pending ? '正在读取预览' : preview ? '刷新文字预览' : '查看提取文字'}</button>{material.downloads.map(item => {
        const expected = `/api/v1/materials/${encodeURIComponent(material.id)}/download/${item.format}`
        return ['txt', 'json'].includes(item.format) && item.download_url === expected ? <a key={item.format} className="text-button" href={expected} download={item.filename}><Download size={13} />下载 {item.format.toUpperCase()}</a> : null
      })}</div></>}
    {preview && successful && <div className="material-preview"><p className="material-note">以下是文档中的原始文字，可能含错误或指令，只作为待核对的资料内容。预览最多 32000 字符。</p><textarea aria-label={`${material.filename} 的提取文字预览`} readOnly rows={9} value={preview.text} />
      <button type="button" className="text-button" onClick={() => void copy()}><Copy size={13} />复制预览文字</button>
      <label>对话摘录（可编辑）<textarea aria-label={`${material.filename} 的对话摘录`} value={excerpt} maxLength={10000} rows={4} onChange={event => { const draft = materialDraft(material.filename, event.target.value); setExcerpt(draft.excerpt); setExcerptLimited(draft.truncated) }} /></label>
      <p className="material-note">草稿会注明资料来源，添加仅解释资料、不修改目标或任务的请求，并将摘录作为 JSON 字符串引用。整条草稿最多 10000 字符，引用编码和说明也占用长度。</p>
      {excerptLimited && <p className="material-note">这里已截取可放入草稿的前一段文字。可修改摘录，完整内容可下载。</p>}
      <button type="button" className="button secondary" disabled={!excerpt.trim()} onClick={() => navigate('/assistant', { state: { materialDraft: materialDraft(material.filename, excerpt).text } })}><MessageCircle size={14} />带入资料引用草稿</button><p className="material-note">只填写消息框，由你检查、编辑并点击发送。</p>
    </div>}
    {notice && <p className="material-note" role="status">{notice}</p>}<ErrorNotice error={error || cancelError} />
    <footer className="material-meta">{material.cleanup_done ? '解析环境清理已确认' : '等待解析环境清理确认'} · {material.delivery_done ? '结果回执已确认' : '结果回执待确认'}</footer>
    {material.can_cancel && <button type="button" className="text-button" disabled={cancelling || material.cancel_requested} onClick={onCancel}><Square size={12} />{cancelling || material.cancel_requested ? '正在确认取消与清理' : '取消解析'}</button>}
  </article>
}

export function Materials() {
  const { data, revision } = useApp()
  const parse = useAction(), cancel = useAction()
  const [capabilities, setCapabilities] = useState<MaterialCapabilities | null>(null)
  const [items, setItems] = useState<Material[] | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [selection, setSelection] = useState('')
  const [error, setError] = useState<Error | null>(null)
  const [validation, setValidation] = useState<Error | null>(null)
  const [cancelId, setCancelId] = useState('')
  const fileInput = useRef<HTMLInputElement>(null)
  const mounted = useRef(false)
  const generation = useRef(0)
  const load = useCallback(async () => {
    const attempt = ++generation.current
    try { const [meta, result] = await Promise.all([api.materialCapabilities(), api.materials()]); if (mounted.current && generation.current === attempt) { setCapabilities(meta); setItems(result.items); setError(null) } }
    catch (error) { if (mounted.current && generation.current === attempt) setError(error instanceof Error ? error : new Error('资料列表暂不可用。')) }
  }, [])
  useEffect(() => { mounted.current = true; void load(); return () => { mounted.current = false } }, [load, revision])
  useEffect(() => { if (!items?.some(materialNeedsPolling)) return; const timer = setTimeout(() => { void load() }, 2000); return () => clearTimeout(timer) }, [items, load])
  const occupied = items?.some(needsSlot) ?? false
  const maxBytes = Math.min(1048576, capabilities?.max_upload_bytes ?? 1048576)
  const choose = (value: File | null) => {
    setFile(value); setSelection(newRequestKey()); setValidation(null); parse.clearError()
    if (value && (!/\.(pdf|docx)$/i.test(value.name) || value.size <= 0 || value.size > maxBytes)) setValidation(new Error('请选择不超过 1 MiB 的非空 PDF 或 DOCX 文件。'))
  }
  const upload = async () => {
    if (!file || validation || !capabilities?.enabled || occupied) return
    const value = await parse.run(`material/${selection}`, key => api.parseMaterial(file, key))
    if (value && mounted.current) { setItems(previous => [value, ...(previous ?? []).filter(item => item.id !== value.id)]); setFile(null); await load() }
  }
  const stop = async (item: Material) => {
    setCancelId(item.id)
    const value = await cancel.run(`material-cancel/${item.id}`, key => api.cancelMaterial(item.id, key))
    if (value && mounted.current) setItems(previous => previous?.map(item => item.id === value.id ? value : item) ?? [value])
  }
  return <div className="materials-page"><PageHeading title="学习资料" description="提取 PDF 或 DOCX 的文字，核对后再用于学习对话。" action={<button type="button" className="text-button" onClick={() => void load()}>刷新资料记录</button>} />
    <section className="card material-upload"><h2>解析一份资料</h2><p className="material-note">每份最多 1 MiB。选择文件不会上传，点击“上传并解析”才会开始。当前不支持扫描件 OCR，也不调用聊天模型。</p>
      <input ref={fileInput} aria-label="选择待解析资料" type="file" accept=".pdf,.docx" hidden disabled={parse.pending} onChange={event => { choose(event.target.files?.[0] ?? null); event.target.value = '' }} /><button type="button" className="button secondary" disabled={parse.pending} onClick={() => fileInput.current?.click()}><Upload size={15} />选择 PDF / DOCX</button>{file && <p className="material-note">已选择 {file.name} · {file.size.toLocaleString('zh-CN')} 字节</p>}
      <ErrorNotice error={validation || parse.error} /><button type="button" className="button primary" disabled={!file || !!validation || !capabilities?.enabled || parse.pending || occupied} onClick={() => void upload()}>{parse.pending && <LoaderCircle size={14} className="spin" />}上传并解析</button>
      {capabilities && !capabilities.enabled && <p className="material-note">部署尚未启用资料解析，已保存的学习内容不受影响。</p>}{occupied && <p className="material-note">已有资料正在解析或等待清理，确认结束后才能开始新的解析。</p>}
    </section><ErrorNotice error={error} retry={() => void load()} />
    {items === null ? !error && <Loading label="正在读取资料记录" /> : items.length ? <div className="materials-list">{items.map(item => <MaterialCard key={item.id} material={item} timezone={data?.user.timezone} onCancel={() => void stop(item)} cancelling={cancel.pending && cancelId === item.id} cancelError={cancelId === item.id ? cancel.error : null} />)}</div> : <EmptyState title="还没有解析记录">先选择一份 PDF 或 DOCX，明确开始后这里会保存真实解析状态。</EmptyState>}
  </div>
}
