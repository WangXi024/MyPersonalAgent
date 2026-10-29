/** Stateful linear resampling preserves phase and the boundary sample between capture blocks. */
export class PcmResampler {
  private tail = new Float32Array(0)
  private position = 0
  constructor(private inputRate: number, private outputRate: number) {
    if (![inputRate, outputRate].every(rate => Number.isFinite(rate) && rate >= 8000 && rate <= 192000)) throw new Error('音频采样率无效。')
  }
  push(samples: Float32Array): ArrayBuffer {
    if (this.inputRate === this.outputRate) return encodePcm16(samples)
    const input = new Float32Array(this.tail.length + samples.length)
    input.set(this.tail); input.set(samples, this.tail.length)
    const output: number[] = []
    const step = this.inputRate / this.outputRate
    while (this.position + 1 < input.length) {
      const index = Math.floor(this.position), fraction = this.position - index
      output.push(input[index] * (1 - fraction) + input[index + 1] * fraction)
      this.position += step
    }
    const consumed = Math.min(Math.floor(this.position), Math.max(0, input.length - 1))
    this.tail = input.slice(consumed)
    this.position -= consumed
    return encodePcm16(Float32Array.from(output))
  }
}

export function encodePcm16(samples: Float32Array): ArrayBuffer {
  const bytes = new ArrayBuffer(samples.length * 2), view = new DataView(bytes)
  samples.forEach((sample, index) => {
    const value = Number.isFinite(sample) ? Math.max(-1, Math.min(1, sample)) : 0
    view.setInt16(index * 2, Math.round(value < 0 ? value * 32768 : value * 32767), true)
  })
  return bytes
}

export function decodePcm16(bytes: ArrayBuffer): Float32Array {
  if (bytes.byteLength % 2) throw new Error('语音数据长度无效。')
  const view = new DataView(bytes), samples = new Float32Array(bytes.byteLength / 2)
  for (let index = 0; index < samples.length; index++) {
    const value = view.getInt16(index * 2, true)
    samples[index] = value / (value < 0 ? 32768 : 32767)
  }
  return samples
}

export class PcmFrames {
  private pending = new Uint8Array(0)
  private frameBytes: number
  constructor(rate: number, maxBytes = 65536) {
    if (!Number.isFinite(rate) || rate < 8000 || rate > 192000 || maxBytes < 2) throw new Error('音频帧配置无效。')
    this.frameBytes = Math.min(Math.max(2, Math.round(rate / 10) * 2), maxBytes - maxBytes % 2)
  }
  push(bytes: ArrayBuffer): ArrayBuffer[] {
    const combined = new Uint8Array(this.pending.length + bytes.byteLength)
    combined.set(this.pending); combined.set(new Uint8Array(bytes), this.pending.length)
    const frames: ArrayBuffer[] = []
    let offset = 0
    while (offset + this.frameBytes <= combined.length) {
      frames.push(combined.slice(offset, offset + this.frameBytes).buffer)
      offset += this.frameBytes
    }
    this.pending = combined.slice(offset)
    return frames
  }
}
