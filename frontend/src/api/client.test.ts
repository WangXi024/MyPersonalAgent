import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, request, setCsrfToken } from './client'

afterEach(() => { vi.unstubAllGlobals(); setCsrfToken('') })
describe('API boundary', () => {
  it('uploads materials as multipart without altering the general submission attachment endpoint', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{}'))
    vi.stubGlobal('fetch', fetcher); setCsrfToken('material-csrf')
    const file = new File(['synthetic'], 'lesson.pdf', { type: 'application/pdf' })
    await api.parseMaterial(file, 'material-request')
    expect(fetcher.mock.calls[0][0]).toBe('/api/v1/materials')
    expect(fetcher.mock.calls[0][1].body).toBeInstanceOf(FormData)
    expect(fetcher.mock.calls[0][1].headers['Content-Type']).toBeUndefined()
    expect(fetcher.mock.calls[0][1].headers['X-CSRF-Token']).toBe('material-csrf')
    expect(fetcher.mock.calls[0][1].headers['Idempotency-Key']).toBe('material-request')
    await api.upload(file, 'submission-upload')
    expect(fetcher.mock.calls[1][0]).toBe('/api/v1/attachments')
  })
  it('submits practice choices only to the scoped operation with CSRF and a stable caller key', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{}'))
    vi.stubGlobal('fetch', fetcher); setCsrfToken('quiz-csrf')
    await api.submitQuizAttempt('op/1', 'quiz-1', [{ question_id: 'q1', choice_id: 'a' }], 'practice-request')
    expect(fetcher).toHaveBeenCalledWith('/api/v1/operations/op%2F1/quizzes/quiz-1/attempts', expect.objectContaining({ credentials: 'same-origin', method: 'POST', headers: expect.objectContaining({ 'X-CSRF-Token': 'quiz-csrf', 'Idempotency-Key': 'practice-request' }), body: JSON.stringify({ answers: [{ question_id: 'q1', choice_id: 'a' }] }) }))
  })
  it('downloads only trusted Markdown responses using the existing same-origin login cookie', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('# 学习记录', { headers: { 'Content-Type': 'text/markdown; charset=utf-8', 'Content-Disposition': 'attachment; filename="learning-report-goal-1.md"' } }))
    vi.stubGlobal('fetch', fetcher)
    const result = await api.learningReport('goal-1')
    expect(fetcher).toHaveBeenCalledWith('/api/v1/goals/goal-1/report.md', expect.objectContaining({ credentials: 'same-origin', headers: { Accept: 'text/markdown' } }))
    expect(result.filename).toBe('learning-report-goal-1.md'); expect(result.blob.size).toBeGreaterThan(0)
    fetcher.mockResolvedValue(new Response('<html>login</html>', { headers: { 'Content-Type': 'text/html' } }))
    await expect(api.learningReport('goal-1')).rejects.toThrow('报告响应格式不正确')
  })
  it('sends same-origin cookies, CSRF and the supplied idempotency key', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: 'accepted' }), { status: 202 }))
    vi.stubGlobal('fetch', fetcher)
    setCsrfToken('test-only-csrf')
    await request('/goals', { method: 'POST', body: { title: '学习' }, key: 'test-retry-key' })
    expect(fetcher).toHaveBeenCalledWith('/api/v1/goals', expect.objectContaining({ credentials: 'same-origin', method: 'POST', headers: expect.objectContaining({ 'X-CSRF-Token': 'test-only-csrf', 'Idempotency-Key': 'test-retry-key' }) }))
  })
  it('does not supply a JSON content type for multipart attachments', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{}'))
    vi.stubGlobal('fetch', fetcher)
    const body = new FormData()
    body.append('file', new File(['learning'], 'notes.txt'))
    await request('/attachments', { method: 'POST', body, key: 'upload-test' })
    expect(fetcher.mock.calls[0][1].headers['Content-Type']).toBeUndefined()
    expect(fetcher.mock.calls[0][1].body).toBe(body)
  })
  it('preserves meaningful server errors and request ids', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: 'stale_version', message: '计划已经更新', request_id: 'request-test' }), { status: 409 })))
    await expect(request('/plans/p/accept', { method: 'POST', body: {} })).rejects.toMatchObject({ status: 409, code: 'stale_version', message: '计划已经更新', requestId: 'request-test' })
  })
  it('turns network errors into a failure, never a fabricated success', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('connection failed')))
    await expect(request('/bootstrap')).rejects.toBeInstanceOf(ApiError)
  })
})
