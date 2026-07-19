class JixiaPcmRingPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.generation = 0;
    this.capacity = Math.max(128, Math.round(sampleRate * 3));
    this.minimumThreshold = Math.max(128, Math.round(sampleRate * 0.08));
    this.maximumThreshold = Math.max(this.minimumThreshold, Math.round(sampleRate * 0.24));
    this.startThreshold = Math.max(this.minimumThreshold, Math.round(sampleRate * 0.1));
    this.ring = new Float32Array(this.capacity);
    this.readIndex = 0;
    this.writeIndex = 0;
    this.available = 0;
    this.primed = false;
    this.ended = false;
    this.underrunReported = false;
    this.playedFrames = 0;
    this.reportCountdown = 0;
    this.stableFrames = 0;
    this.underrunCount = 0;
    this.port.onmessage = (event) => this.handleMessage(event.data || {});
  }

  reset(message) {
    this.generation = message.generation || 0;
    this.capacity = Math.max(128, message.capacityFrames || Math.round(sampleRate * 3));
    this.startThreshold = Math.min(
      this.capacity,
      Math.max(this.minimumThreshold, message.startThresholdFrames || Math.round(sampleRate * 0.1)),
    );
    this.maximumThreshold = Math.min(
      this.capacity,
      Math.max(this.startThreshold, message.maximumThresholdFrames || Math.round(sampleRate * 0.24)),
    );
    this.ring = new Float32Array(this.capacity);
    this.readIndex = 0;
    this.writeIndex = 0;
    this.available = 0;
    this.primed = false;
    this.ended = false;
    this.underrunReported = false;
    this.playedFrames = 0;
    this.reportCountdown = 0;
    this.stableFrames = 0;
    this.underrunCount = 0;
  }

  handleMessage(message) {
    if (message.type === "reset") {
      this.reset(message);
      return;
    }
    if (message.generation !== this.generation) return;
    if (message.type === "flush") {
      this.reset({ generation: this.generation, capacityFrames: this.capacity, startThresholdFrames: this.startThreshold });
      return;
    }
    if (message.type === "end") {
      this.ended = true;
      if (!this.primed && this.available > 0) {
        this.primed = true;
        this.underrunReported = false;
        this.port.postMessage({ type: "started", generation: this.generation, bufferedFrames: this.available, playedFrames: this.playedFrames });
      }
      return;
    }
    if (message.type !== "enqueue" || !(message.frames instanceof Float32Array)) return;
    const frames = message.frames;
    if (frames.length > this.capacity - this.available) {
      this.port.postMessage({ type: "overflow", generation: this.generation, bufferedFrames: this.available, playedFrames: this.playedFrames });
      return;
    }
    for (let index = 0; index < frames.length; index += 1) {
      this.ring[this.writeIndex] = frames[index];
      this.writeIndex = (this.writeIndex + 1) % this.capacity;
    }
    this.available += frames.length;
    this.port.postMessage({
      type: "buffer",
      generation: this.generation,
      bufferedFrames: this.available,
      playedFrames: this.playedFrames,
    });
    if (!this.primed && (this.available >= this.startThreshold || (this.ended && this.available > 0))) {
      this.primed = true;
      this.underrunReported = false;
      this.port.postMessage({ type: "started", generation: this.generation, bufferedFrames: this.available, playedFrames: this.playedFrames });
    }
  }

  process(_inputs, outputs) {
    const output = outputs[0] && outputs[0][0];
    if (!output) return true;
    if (!this.primed) {
      if (this.ended && this.available === 0) {
        this.port.postMessage({ type: "drained", generation: this.generation, bufferedFrames: 0, playedFrames: this.playedFrames });
        this.ended = false;
      }
      return true;
    }
    const readable = Math.min(output.length, this.available);
    for (let index = 0; index < readable; index += 1) {
      output[index] = this.ring[this.readIndex];
      this.readIndex = (this.readIndex + 1) % this.capacity;
    }
    this.available -= readable;
    this.playedFrames += readable;
    if (readable < output.length) {
      this.primed = false;
      if (this.ended) {
        this.ended = false;
        this.port.postMessage({ type: "drained", generation: this.generation, bufferedFrames: 0, playedFrames: this.playedFrames });
      } else if (!this.underrunReported) {
        this.underrunReported = true;
        this.underrunCount += 1;
        this.stableFrames = 0;
        this.startThreshold = Math.min(this.maximumThreshold, this.startThreshold + Math.round(sampleRate * 0.04));
        this.port.postMessage({
          type: "underrun",
          generation: this.generation,
          bufferedFrames: 0,
          playedFrames: this.playedFrames,
          targetBufferFrames: this.startThreshold,
          underrunCount: this.underrunCount,
        });
      }
    } else {
      this.stableFrames += readable;
      if (this.stableFrames >= Math.round(sampleRate * 15) && this.startThreshold > this.minimumThreshold) {
        this.stableFrames = 0;
        this.startThreshold = Math.max(this.minimumThreshold, this.startThreshold - Math.round(sampleRate * 0.02));
        this.port.postMessage({
          type: "buffer_target",
          generation: this.generation,
          bufferedFrames: this.available,
          playedFrames: this.playedFrames,
          targetBufferFrames: this.startThreshold,
          underrunCount: this.underrunCount,
        });
      }
    }
    this.reportCountdown -= output.length;
    if (this.reportCountdown <= 0) {
      this.reportCountdown = Math.round(sampleRate * 0.25);
      this.port.postMessage({ type: "buffer", generation: this.generation, bufferedFrames: this.available, playedFrames: this.playedFrames });
    }
    return true;
  }
}

registerProcessor("jixia-pcm-ring-player", JixiaPcmRingPlayer);
