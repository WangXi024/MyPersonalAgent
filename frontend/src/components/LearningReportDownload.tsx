import { useEffect, useRef, useState } from 'react'
import { Download, LoaderCircle } from 'lucide-react'
import { api } from '../api/client'
import { ErrorNotice } from './ui'
import './learning-report.css'

export function LearningReportDownload({ goalId }: { goalId: string }) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const request = useRef<AbortController | null>(null)
  const downloads = useRef(new Map<string, ReturnType<typeof setTimeout>>())
  useEffect(() => {
    const urls = downloads.current
    setPending(false); setError(null)
    return () => { request.current?.abort(); request.current = null; for (const [url, timer] of urls) { clearTimeout(timer); URL.revokeObjectURL(url) }; urls.clear() }
  }, [goalId])
  const download = async () => {
    if (request.current) return
    const control = new AbortController(); request.current = control; setPending(true); setError(null)
    try {
      const result = await api.learningReport(goalId, control.signal)
      if (control.signal.aborted) return
      const url = URL.createObjectURL(result.blob), link = document.createElement('a')
      downloads.current.set(url, setTimeout(() => { URL.revokeObjectURL(url); downloads.current.delete(url) }, 1000))
      link.href = url; link.download = result.filename; link.hidden = true; document.body.append(link)
      try { link.click() } finally { link.remove() }
    } catch (error) { if (!control.signal.aborted) setError(error instanceof Error ? error : new Error('报告暂时无法下载。')) }
    finally { if (!control.signal.aborted) { request.current = null; setPending(false) } }
  }
  return <div className="learning-report-download"><button type="button" className="button secondary" disabled={pending} onClick={() => void download()}>{pending ? <LoaderCircle size={15} className="spin" /> : <Download size={15} />}下载学习报告</button><ErrorNotice error={error} /></div>
}
