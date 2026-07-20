class JixiaLiveKitInterruptGate extends AudioWorkletProcessor {
  constructor() {
    super();
    // WebRTC generation switches, mute changes and hard interrupts can land
    // on any sample.  Jumping directly between zero and a non-zero waveform
    // creates a short click/pop even when RTP delivery is otherwise perfect.
    // A five-millisecond gain ramp is below perceptible speech latency, does
    // not buffer audio and removes that discontinuity from the rendered edge.
    this.declickFrames = Math.max(1, Math.round(sampleRate * 0.005));
    this.outputGain = 1;
    this.targetGain = 1;
    this.gainRampFrames = 0;
    this.epoch = 0;
    this.blocked = false;
    this.muted = false;
    this.guardFrames = 0;
    this.pendingActivation = false;
    this.activeGeneration = "";
    this.telemetrySamples = 0;
    this.telemetrySquareSum = 0;
    this.telemetryPeak = 0;
    this.telemetryAudible = false;
    this.telemetryAudibleWindows = 0;
    this.telemetrySilentWindows = 0;
    this.telemetryWindowSamples = Math.max(128, Math.round(sampleRate * 0.02));
    this.port.onmessage = (event) => this.handleMessage(event.data || {});
  }

  updateTargetGain() {
    const target = this.muted || this.blocked ? 0 : 1;
    if (target === this.targetGain && this.gainRampFrames > 0) return;
    this.targetGain = target;
    if (Math.abs(this.outputGain - target) < 1e-6) {
      this.outputGain = target;
      this.gainRampFrames = 0;
      return;
    }
    this.gainRampFrames = this.declickFrames;
  }

  handleMessage(message) {
    if (message.type === "muted") {
      this.muted = Boolean(message.muted);
      this.updateTargetGain();
      this.port.postMessage({
        type: "state",
        action: "muted",
        muted: this.muted,
        blocked: this.blocked,
        epoch: this.epoch,
        generation: this.activeGeneration,
      });
      return;
    }
    const epoch = Number(message.epoch) || 0;
    if (epoch < this.epoch) return;
    this.epoch = epoch;
    if (message.type === "flush") {
      // Keep consuming the remote WebRTC track while outputting zeroes.  This
      // drops the browser jitter-buffer tail instead of pausing a media element
      // and allowing an old generation to resume when it is reattached.
      this.blocked = true;
      this.pendingActivation = false;
      this.guardFrames = Math.max(0, Number(message.guardFrames) || 0);
      this.updateTargetGain();
      this.port.postMessage({
        type: "state",
        action: "flush",
        muted: this.muted,
        blocked: this.blocked,
        epoch: this.epoch,
        generation: this.activeGeneration,
        guardFrames: this.guardFrames,
      });
      return;
    }
    if (message.type === "activate") {
      this.activeGeneration = String(message.generation || "");
      if (this.blocked && this.guardFrames > 0) {
        this.pendingActivation = true;
      } else {
        this.blocked = false;
        this.pendingActivation = false;
        this.updateTargetGain();
      }
      this.port.postMessage({
        type: "state",
        action: "activate",
        muted: this.muted,
        blocked: this.blocked,
        epoch: this.epoch,
        generation: this.activeGeneration,
        pendingActivation: this.pendingActivation,
        guardFrames: this.guardFrames,
      });
    }
  }

  process(inputs, outputs) {
    const input = inputs[0] || [];
    const output = outputs[0] || [];
    const frames = output[0]?.length || 128;
    let unblockAfterQuantum = false;
    if (this.blocked && this.guardFrames > 0) {
      this.guardFrames = Math.max(0, this.guardFrames - frames);
      if (this.guardFrames === 0) {
        this.port.postMessage({ type: "flushed", epoch: this.epoch });
        if (this.pendingActivation) {
          // Keep this render quantum fully inside the interrupt guard.  The
          // following quantum fades the new generation in from zero.
          unblockAfterQuantum = true;
        }
      }
    }
    for (let channel = 0; channel < output.length; channel += 1) {
      const target = output[channel];
      if (!target) continue;
      const source = input[channel] || input[0];
      for (let index = 0; index < target.length; index += 1) {
        if (channel === 0 && this.gainRampFrames > 0) {
          this.outputGain += (this.targetGain - this.outputGain) / this.gainRampFrames;
          this.gainRampFrames -= 1;
          if (this.gainRampFrames === 0) this.outputGain = this.targetGain;
        }
        target[index] = source ? source[index] * this.outputGain : 0;
      }
    }
    if (unblockAfterQuantum) {
      this.blocked = false;
      this.pendingActivation = false;
      this.updateTargetGain();
    }
    const rendered = output[0];
    if (rendered) {
      for (let index = 0; index < rendered.length; index += 1) {
        const value = rendered[index];
        this.telemetrySquareSum += value * value;
        this.telemetryPeak = Math.max(this.telemetryPeak, Math.abs(value));
      }
      this.telemetrySamples += rendered.length;
      if (this.telemetrySamples >= this.telemetryWindowSamples) {
        const rms = Math.sqrt(this.telemetrySquareSum / this.telemetrySamples);
        if (rms >= 0.003) {
          this.telemetryAudibleWindows += 1;
          this.telemetrySilentWindows = 0;
        } else if (rms <= 0.001) {
          this.telemetrySilentWindows += 1;
          this.telemetryAudibleWindows = 0;
        } else {
          this.telemetryAudibleWindows = 0;
          this.telemetrySilentWindows = 0;
        }
        if (!this.telemetryAudible && this.telemetryAudibleWindows >= 3) {
          this.telemetryAudible = true;
          this.port.postMessage({
            type: "audibility",
            audible: true,
            epoch: this.epoch,
            generation: this.activeGeneration,
            rms,
            peak: this.telemetryPeak,
            audioTime: currentTime,
          });
        } else if (this.telemetryAudible && this.telemetrySilentWindows >= 8) {
          this.telemetryAudible = false;
          this.port.postMessage({
            type: "audibility",
            audible: false,
            epoch: this.epoch,
            generation: this.activeGeneration,
            rms,
            peak: this.telemetryPeak,
            audioTime: currentTime,
          });
        }
        this.telemetrySamples = 0;
        this.telemetrySquareSum = 0;
        this.telemetryPeak = 0;
      }
    }
    return true;
  }
}

registerProcessor("jixia-livekit-interrupt-gate", JixiaLiveKitInterruptGate);
