import { act, renderHook } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { useAction } from './hooks'
vi.mock('./state', () => ({ useApp: () => ({ refresh: vi.fn().mockResolvedValue(undefined) }) }))

describe('commands', () => {
  it('retains the same key on failure and changes it when the content changes', async () => {
    const { result } = renderHook(() => useAction())
    const action = vi.fn().mockRejectedValueOnce(new Error('离线')).mockResolvedValueOnce({ operation_id: 'operation-one' }).mockResolvedValue({ operation_id: 'operation-two' })
    await act(async () => { await result.current.run('same-message', action) })
    expect(result.current.error?.message).toBe('离线')
    await act(async () => { await result.current.run('same-message', action) })
    expect(action.mock.calls[0][0]).toBe(action.mock.calls[1][0])
    expect(result.current.error).toBeNull()
    await act(async () => { await result.current.run('edited-message', action) })
    expect(action.mock.calls[2][0]).not.toBe(action.mock.calls[1][0])
  })
  it('blocks a second click while the same command is pending', async () => {
    const { result } = renderHook(() => useAction())
    let resolve: (value: object) => void = () => undefined
    const action = vi.fn().mockImplementation(() => new Promise(done => { resolve = done }))
    let first: Promise<unknown>
    await act(async () => { first = result.current.run('command', action); await result.current.run('command', action) })
    expect(action).toHaveBeenCalledOnce()
    await act(async () => { resolve({ ok: true }); await first })
    expect(result.current.pending).toBe(false)
  })
  it('retries the original command snapshot if background versions change', async () => {
    const { result } = renderHook(() => useAction())
    const firstSnapshot = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue({ accepted: true })
    const changedSnapshot = vi.fn()
    await act(async () => { await result.current.run('same-user-input', firstSnapshot) })
    await act(async () => { await result.current.run('same-user-input', changedSnapshot) })
    expect(firstSnapshot).toHaveBeenCalledTimes(2)
    expect(changedSnapshot).not.toHaveBeenCalled()
  })

})
