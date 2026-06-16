/* Browser audio helpers for the ASR/TTS testers. */

function mergeFloat32(chunks: Float32Array[]): Float32Array {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const out = new Float32Array(total);
  let off = 0;
  for (const c of chunks) {
    out.set(c, off);
    off += c.length;
  }
  return out;
}

function downsample(input: Float32Array, inRate: number, outRate: number): Float32Array {
  if (outRate >= inRate) return input;
  const ratio = inRate / outRate;
  const outLen = Math.floor(input.length / ratio);
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++) {
    out[i] = input[Math.floor(i * ratio)];
  }
  return out;
}

function floatToPcm16(input: Float32Array): Int16Array {
  const out = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

function bytesToBase64(bytes: Uint8Array): string {
  let bin = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
}

export interface PcmRecorder {
  analyser: AnalyserNode;
  stop: () => Promise<{ base64: string; durationMs: number; samples: number }>;
}

/** Start recording mic audio; stop() returns 16kHz mono L16 PCM as base64. */
export async function startPcmRecorder(): Promise<PcmRecorder> {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });
  const ac = new AudioContext();
  const source = ac.createMediaStreamSource(stream);
  const analyser = ac.createAnalyser();
  analyser.fftSize = 512;
  const processor = ac.createScriptProcessor(4096, 1, 1);
  const silent = ac.createGain();
  silent.gain.value = 0; // keep graph alive without echoing mic to speakers
  const chunks: Float32Array[] = [];

  source.connect(analyser);
  source.connect(processor);
  processor.connect(silent);
  silent.connect(ac.destination);
  processor.onaudioprocess = (e) => {
    chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  };

  return {
    analyser,
    async stop() {
      processor.onaudioprocess = null;
      processor.disconnect();
      source.disconnect();
      analyser.disconnect();
      silent.disconnect();
      stream.getTracks().forEach((t) => t.stop());
      const inRate = ac.sampleRate;
      await ac.close();
      const flat = mergeFloat32(chunks);
      const down = downsample(flat, inRate, 16000);
      const pcm = floatToPcm16(down);
      return {
        base64: bytesToBase64(new Uint8Array(pcm.buffer)),
        durationMs: Math.round((flat.length / inRate) * 1000),
        samples: down.length,
      };
    },
  };
}

export interface PcmStream {
  analyser: AnalyserNode;
  stop: () => Promise<void>;
}

/** Stream mic audio as 16kHz mono L16 PCM frames (~40ms each) to `onFrame`. */
export async function startPcmStream(onFrame: (frame: ArrayBuffer) => void): Promise<PcmStream> {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });
  const ac = new AudioContext();
  const source = ac.createMediaStreamSource(stream);
  const analyser = ac.createAnalyser();
  analyser.fftSize = 512;
  const processor = ac.createScriptProcessor(4096, 1, 1);
  const silent = ac.createGain();
  silent.gain.value = 0;
  const inRate = ac.sampleRate;
  const FRAME = 640; // 640 samples @16kHz = 1280 bytes = 40ms
  let acc = new Int16Array(0);

  source.connect(analyser);
  source.connect(processor);
  processor.connect(silent);
  silent.connect(ac.destination);
  processor.onaudioprocess = (e) => {
    const pcm = floatToPcm16(downsample(e.inputBuffer.getChannelData(0), inRate, 16000));
    const merged = new Int16Array(acc.length + pcm.length);
    merged.set(acc);
    merged.set(pcm, acc.length);
    acc = merged;
    while (acc.length >= FRAME) {
      const frame = acc.slice(0, FRAME);
      onFrame(frame.buffer);
      acc = acc.slice(FRAME);
    }
  };

  return {
    analyser,
    async stop() {
      processor.onaudioprocess = null;
      processor.disconnect();
      source.disconnect();
      analyser.disconnect();
      silent.disconnect();
      stream.getTracks().forEach((t) => t.stop());
      await ac.close();
    },
  };
}

/**
 * Progressive audio player. Pushes encoded audio chunks and plays them as they
 * arrive via MediaSource; falls back to buffer-then-play if MSE/codec is
 * unsupported.
 */
export function createStreamingPlayer(mime: string) {
  const canStream =
    typeof MediaSource !== "undefined" && MediaSource.isTypeSupported(mime) && mime === "audio/mpeg";
  const audio = new Audio();
  const buffered: Uint8Array[] = [];

  if (canStream) {
    const ms = new MediaSource();
    audio.src = URL.createObjectURL(ms);
    let sb: SourceBuffer | null = null;
    const queue: Uint8Array[] = [];
    let ended = false;
    const pump = () => {
      if (!sb || sb.updating) return;
      const next = queue.shift();
      if (next) sb.appendBuffer(next as unknown as BufferSource);
      else if (ended && ms.readyState === "open") {
        try {
          ms.endOfStream();
        } catch {
          /* ignore */
        }
      }
    };
    ms.addEventListener("sourceopen", () => {
      sb = ms.addSourceBuffer(mime);
      sb.addEventListener("updateend", pump);
      pump();
    });
    return {
      push(bytes: Uint8Array) {
        queue.push(bytes);
        void audio.play().catch(() => undefined);
        pump();
      },
      end() {
        ended = true;
        pump();
      },
      element: audio,
    };
  }

  // Fallback: collect then play once.
  return {
    push(bytes: Uint8Array) {
      buffered.push(bytes);
    },
    end() {
      const total = buffered.reduce((n, b) => n + b.length, 0);
      const all = new Uint8Array(total);
      let off = 0;
      for (const b of buffered) {
        all.set(b, off);
        off += b.length;
      }
      audio.src = URL.createObjectURL(new Blob([all], { type: mime }));
      void audio.play().catch(() => undefined);
    },
    element: audio,
  };
}

export function base64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** Play base64-encoded audio (e.g. TTS probe output) on the local speakers. */
export function playBase64Audio(base64: string, mime = "audio/mpeg"): Promise<void> {
  const bin = atob(base64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const url = URL.createObjectURL(new Blob([bytes], { type: mime }));
  const audio = new Audio(url);
  return audio
    .play()
    .then(
      () =>
        new Promise<void>((resolve) => {
          audio.onended = () => {
            URL.revokeObjectURL(url);
            resolve();
          };
        })
    )
    .catch((err) => {
      URL.revokeObjectURL(url);
      throw err;
    });
}
