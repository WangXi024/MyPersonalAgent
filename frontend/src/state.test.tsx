import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from './api/client'
import { openEventStream } from './events/stream'
import { AppProvider, useApp } from './state'
import { bootstrap } from './test/fixtures'
vi.mock('./events/stream', () => ({ openEventStream: vi.fn().mockReturnValue(() => undefined) }))
let state: ReturnType<typeof useApp>
function Probe() { state = useApp(); return <div><span data-testid="cursor">{state.data?.cursor ?? 'loading'}</span><span data-testid="error">{state.error?.message}</span></div> }
beforeEach(() => { vi.mocked(openEventStream).mockClear(); vi.spyOn(api, 'me').mockResolvedValue({ user: bootstrap.user, csrf_token: 'test-only' }); vi.spyOn(api, 'bootstrap').mockResolvedValue({ ...bootstrap, cursor: 1 }) })
afterEach(() => vi.useRealTimers())
async function mount() { render(<AppProvider><Probe /></AppProvider>); await waitFor(() => expect(screen.getByTestId('cursor')).toHaveTextContent('1')) }
describe('snapshot and SSE coordination', () => {
  it('waits for the queued snapshot before reopening an expired stream', async () => {
    await mount()
    let resolveSnapshot: (value: typeof bootstrap) => void = () => undefined
    vi.mocked(api.bootstrap).mockImplementationOnce(() => new Promise(resolve => { resolveSnapshot = resolve })).mockResolvedValueOnce({ ...bootstrap, cursor: 3 })
    let pending: Promise<void>
    await act(async () => { pending = state.refresh() })
    act(() => vi.mocked(openEventStream).mock.calls[0][3]())
    expect(openEventStream).toHaveBeenCalledTimes(1)
    await act(async () => { resolveSnapshot({ ...bootstrap, cursor: 2 }); await pending })
    expect(screen.getByTestId('cursor')).toHaveTextContent('3')
    expect(openEventStream).toHaveBeenCalledTimes(2)
    expect(vi.mocked(openEventStream).mock.calls[1][0]).toBe(3)
  })
  it('retries a failed final-event snapshot without waiting for another SSE event', async () => {
    await mount()
    vi.useFakeTimers()
    vi.mocked(api.bootstrap).mockRejectedValueOnce(new ApiError(503, '暂时不可用')).mockResolvedValueOnce({ ...bootstrap, cursor: 2 })
    await act(async () => { vi.mocked(openEventStream).mock.calls[0][1]({ seq: 2, type: 'operation.succeeded', summary: '完成', created_at: '' }); await Promise.resolve(); await Promise.resolve() })
    expect(screen.getByTestId('cursor')).toHaveTextContent('1')
    expect(screen.getByTestId('error')).toHaveTextContent('暂时不可用')
    await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
    expect(screen.getByTestId('cursor')).toHaveTextContent('2')
    expect(screen.getByTestId('error')).toBeEmptyDOMElement()
  })
})
