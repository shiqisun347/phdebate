class JixiaAsrPcmCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const requestedChunkMs = Number(options?.processorOptions?.chunkMs) || 20;
    this.chunkFrames = Math.max(128, Math.round(sampleRate * requestedChunkMs / 1000));
    this.pending = new Float32Array(this.chunkFrames);
    this.pendingFrames = 0;
    this.generation = Number(options?.processorOptions?.generation) || 0;
    this.active = true;
    this.port.onmessage = (event) => this.handleMessage(event.data || {});
  }

  emitPending() {
    if (!this.pendingFrames) return;
    const frames = this.pending.slice(0, this.pendingFrames);
    this.pendingFrames = 0;
    this.port.postMessage({ type: "frames", generation: this.generation, frames }, [frames.buffer]);
  }

  handleMessage(message) {
    if (message.generation !== this.generation) return;
    if (message.type === "flush") {
      this.emitPending();
      this.port.postMessage({ type: "flushed", generation: this.generation });
    } else if (message.type === "stop") {
      this.emitPending();
      this.active = false;
      this.port.postMessage({ type: "flushed", generation: this.generation });
    }
  }

  process(inputs, outputs) {
    const output = outputs[0]?.[0];
    if (output) output.fill(0);
    if (!this.active) return true;
    const input = inputs[0]?.[0];
    if (!input?.length) return true;
    let offset = 0;
    while (offset < input.length) {
      const writable = Math.min(input.length - offset, this.chunkFrames - this.pendingFrames);
      this.pending.set(input.subarray(offset, offset + writable), this.pendingFrames);
      this.pendingFrames += writable;
      offset += writable;
      if (this.pendingFrames === this.chunkFrames) this.emitPending();
    }
    return true;
  }
}

registerProcessor("jixia-asr-pcm-capture", JixiaAsrPcmCapture);
