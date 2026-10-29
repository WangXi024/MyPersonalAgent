import { describe, expect, it, vi } from 'vitest'
import { decodePcm16, encodePcm16, PcmFrames, PcmResampler } from './audio'
import { PcmPlayback } from './browser-audio'

describe('PCM capture and playback', () => {
  it('encodes signed little-endian mono PCM, clamps overflow and silences invalid samples', () => {
    const data = encodePcm16(new Float32Array([-2, -0.5, 0, 0.5, 2, NaN, Infinity]))
    const view = new DataView(data)
    expect(Array.from({ length: 7 }, (_, index) => view.getInt16(index * 2, true))).toEqual([-32768, -16384, 0, 16384, 32767, 0, 0])
    expect(new Uint8Array(data).slice(0, 2)).toEqual(new Uint8Array([0, 128]))
    expect(decodePcm16(data)[0]).toBe(-1)
    expect(() => decodePcm16(new ArrayBuffer(3))).toThrow()
  })
  it.each([[48000, 24000], [16000, 24000], [44100, 16000]])('preserves resampler phase across capture chunks at %i to %i Hz', (inputRate, outputRate) => {
    const samples = Float32Array.from({ length: 8192 }, (_, index) => Math.sin(index / 19) * 0.8)
    const continuous = new Int16Array(new PcmResampler(inputRate, outputRate).push(samples))
    const fragmented = new PcmResampler(inputRate, outputRate), pieces: number[] = []
    for (let offset = 0; offset < samples.length; offset += 137) pieces.push(...new Int16Array(fragmented.push(samples.slice(offset, offset + 137))))
    expect(pieces).toHaveLength(continuous.length)
    expect(Math.max(...pieces.map((value, index) => Math.abs(value - continuous[index])))).toBeLessThanOrEqual(1)
  })
  it('batches 100 ms frames at the negotiated rate and keeps the remaining bytes', () => {
    const frames = new PcmFrames(24000)
    expect(frames.push(new ArrayBuffer(4700))).toEqual([])
    expect(frames.push(new ArrayBuffer(5000)).map(frame => frame.byteLength)).toEqual([4800, 4800])
    expect(frames.push(new ArrayBuffer(4700)).map(frame => frame.byteLength)).toEqual([4800])
    expect(new PcmFrames(24000, 101).push(new ArrayBuffer(300)).map(frame => frame.byteLength)).toEqual([100, 100, 100])
    expect(() => new PcmResampler(0, 24000)).toThrow()
  })
  it('counts hardware playback position, including partial playback, and clears every queued source', () => {
    let outputTime = 0
    const nodes: { onended: (() => void) | null; stop: ReturnType<typeof vi.fn>; disconnect: ReturnType<typeof vi.fn> }[] = []
    const context = { currentTime: 0, destination: {}, getOutputTimestamp: () => ({ contextTime: outputTime }),
      createBuffer: (_channels: number, count: number) => ({ getChannelData: () => new Float32Array(count) }),
      createBufferSource: () => { const node = { onended: null, buffer: null, connect: vi.fn(), start: vi.fn(), stop: vi.fn(), disconnect: vi.fn() }; nodes.push(node); return node },
    }
    const playback = new PcmPlayback(context as unknown as AudioContext, 8000)
    playback.enqueue('response', new ArrayBuffer(16000))
    playback.enqueue('response', new ArrayBuffer(16000))
    expect(playback.playedBytes('response')).toBe(0)
    context.currentTime = 1.2; outputTime = 0.51
    // The render thread has ended a node, but the hardware has played only half.
    nodes[0].onended?.()
    expect(playback.playedBytes('response')).toBe(8000)
    playback.clear()
    expect(nodes.every(node => node.stop.mock.calls.length === 1)).toBe(true)
    expect(playback.playedBytes('response')).toBe(8000)
    context.currentTime = 9; outputTime = 9
    expect(playback.playedBytes('response')).toBe(8000)
    playback.close()
  })
})
