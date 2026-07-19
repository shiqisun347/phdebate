export class StreamingLinearResampler {
  private buffer = new Float32Array(0);
  private position = 0;

  constructor(
    readonly inputRate: number,
    readonly outputRate: number,
  ) {
    if (!(inputRate > 0) || !(outputRate > 0)) throw new Error("采样率必须大于零");
  }

  reset() {
    this.buffer = new Float32Array(0);
    this.position = 0;
  }

  process(input: Float32Array): Float32Array {
    if (!input.length) return new Float32Array(0);
    if (this.inputRate === this.outputRate) return input.slice();
    const combined = new Float32Array(this.buffer.length + input.length);
    combined.set(this.buffer);
    combined.set(input, this.buffer.length);
    const ratio = this.inputRate / this.outputRate;
    const output: number[] = [];
    while (this.position < combined.length - 1) {
      const left = Math.floor(this.position);
      const fraction = this.position - left;
      output.push(combined[left] + (combined[left + 1] - combined[left]) * fraction);
      this.position += ratio;
    }
    const consumed = Math.min(Math.floor(this.position), Math.max(0, combined.length - 1));
    this.buffer = combined.slice(consumed);
    this.position -= consumed;
    return Float32Array.from(output);
  }
}
