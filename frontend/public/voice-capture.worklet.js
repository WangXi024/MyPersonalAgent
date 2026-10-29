class LearningVoiceCapture extends AudioWorkletProcessor {
  constructor() {
    super()
    this.pending = new Float32Array(2048)
    this.offset = 0
  }
  process(inputs) {
    const channels = inputs[0]
    if (!channels || !channels.length) return true
    for (let index = 0; index < channels[0].length; index++) {
      let value = 0
      for (const channel of channels) value += channel[index] || 0
      this.pending[this.offset++] = value / channels.length
      if (this.offset === this.pending.length) {
        const block = this.pending
        this.port.postMessage(block, [block.buffer])
        this.pending = new Float32Array(2048)
        this.offset = 0
      }
    }
    return true
  }
}
registerProcessor('learning-voice-capture', LearningVoiceCapture)
