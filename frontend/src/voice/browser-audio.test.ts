import { afterEach, expect, it, vi } from 'vitest'
import { openCapture } from './browser-audio'

afterEach(() => { vi.unstubAllGlobals() })
it('closes AudioContext immediately on cancellation and stops a microphone grant that resolves after leaving', async () => {
  const close = vi.fn().mockResolvedValue(undefined), stop = vi.fn()
  let grant: (stream: unknown) => void = () => undefined
  const getUserMedia = vi.fn().mockImplementation(() => new Promise(resolve => { grant = resolve }))
  vi.stubGlobal('isSecureContext', true)
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia } })
  vi.stubGlobal('AudioWorkletNode', class {})
  vi.stubGlobal('AudioContext', class { state = 'running'; resume = vi.fn().mockResolvedValue(undefined); close = close })
  const abort = new AbortController()
  const attempt = openCapture(vi.fn(), abort.signal)
  // Context.resume is awaited before requesting permission.
  await Promise.resolve(); await Promise.resolve()
  expect(getUserMedia).toHaveBeenCalledOnce()
  abort.abort()
  expect(close).toHaveBeenCalledOnce()
  grant({ getTracks: () => [{ stop }] })
  await expect(attempt).rejects.toMatchObject({ name: 'AbortError' })
  expect(stop).toHaveBeenCalledOnce(); expect(close).toHaveBeenCalledOnce()
})
