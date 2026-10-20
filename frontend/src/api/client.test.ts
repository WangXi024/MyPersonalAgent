import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, request, setCsrfToken } from './client'

afterEach(() => { vi.unstubAllGlobals(); setCsrfToken('') })
describe('API boundary', () => {
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
