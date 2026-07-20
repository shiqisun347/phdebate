(() => {
  if (window.__jixiaRealtimeAudioGate) return;

  const epochNow = () => performance.timeOrigin + performance.now();
  const state = {
    version: 1,
    installedAtMs: epochNow(),
    config: {
      audibleRms: 0.003,
      silenceRms: 0.001,
      audibleFrames: 3,
      silenceFrames: 8,
    },
    roomEvents: [],
    socketUrls: [],
    audio: {
      elementSeenAtMs: null,
      streamSeenAtMs: null,
      contextState: "missing",
      playbackUnlockedAtMs: null,
      playingAtMs: null,
      lastSampleAtMs: null,
      lastRms: 0,
      lastPeak: 0,
      currentlyAudible: false,
      lastAudibleAtMs: null,
      signalPath: "unobserved",
      pendingFirstNonSilent: null,
    },
    turn: null,
    interrupt: null,
    network: {
      samples: [],
      last: null,
    },
    diagnostics: [],
    errors: [],
  };

  let context = null;
  let source = null;
  let analyser = null;
  let silentGain = null;
  let monitoredAudio = null;
  let monitoredStream = null;
  let sampleData = null;
  let audibleFrames = 0;
  let silenceFrames = 0;
  let disposed = false;
  const seenRoomEventSeq = new Set();

  const recordError = (label, error) => {
    state.errors.push({
      atMs: epochNow(),
      label,
      message: error instanceof Error ? error.message : String(error),
    });
  };

  const serializePayload = (payload) => {
    try {
      return JSON.parse(JSON.stringify(payload));
    } catch {
      return { type: String(payload?.type || "unknown") };
    }
  };

  const ingestEvent = (payload) => {
    if (!payload || typeof payload !== "object") return;
    const type = typeof payload.type === "string" ? payload.type : "";
    if (!type) return;
    const receivedAtMs = epochNow();
    state.roomEvents.push({ receivedAtMs, payload: serializePayload(payload) });
    if (state.roomEvents.length > 100) state.roomEvents.splice(0, state.roomEvents.length - 100);

    if (type === "audio.rtc.started") {
      state.turn = {
        generation: String(payload.generation || ""),
        speechId: String(payload.speech_id || ""),
        eventReceivedAtMs: receivedAtMs,
        serverFirstCaptureAt: typeof payload.server_first_capture_at === "string"
          ? payload.server_first_capture_at
          : null,
        agentFirstReadableDeltaAt: typeof payload.agent_first_readable_delta_at === "string"
          ? payload.agent_first_readable_delta_at
          : null,
        firstNonSilentAtMs: null,
        firstNonSilentRms: null,
        firstNonSilentPeak: null,
      };
      const pending = state.audio.pendingFirstNonSilent;
      if (pending && (!pending.generation || pending.generation === state.turn.generation)) {
        state.turn.firstNonSilentAtMs = pending.atMs;
        state.turn.firstNonSilentRms = pending.rms;
        state.turn.firstNonSilentPeak = pending.peak;
        state.audio.pendingFirstNonSilent = null;
      }
      if (state.interrupt && state.interrupt.generation !== state.turn.generation) {
        state.interrupt.newGenerationAtMs = receivedAtMs;
      }
      return;
    }

    if (["audio.rtc.interrupt", "speech.interrupted", "audio.realtime.aborted", "audio.stream.aborted"].includes(type)) {
      const generation = String(payload.generation || state.turn?.generation || "");
      if (state.interrupt?.generation === generation && state.interrupt?.eventType === type) return;
      const lastAudibleAtMs = state.audio.lastAudibleAtMs;
      state.interrupt = {
        eventType: type,
        generation,
        eventReceivedAtMs: receivedAtMs,
        audibleBeforeInterrupt: Boolean(
          state.audio.currentlyAudible
          || (lastAudibleAtMs !== null && receivedAtMs - lastAudibleAtMs <= 500),
        ),
        firstFlushActionAtMs: null,
        flushAction: null,
        sustainedSilenceAtMs: null,
        staleAudioAfterSilenceAtMs: null,
        newGenerationAtMs: null,
      };
      silenceFrames = 0;
    }
  };

  const onWorkletAudibility = (event) => {
    const detail = event?.detail || {};
    const atMs = Number(detail.receivedAtMs) || epochNow();
    const rms = Number(detail.rms) || 0;
    const peak = Number(detail.peak) || 0;
    const generation = String(detail.generation || "");
    state.audio.signalPath = "audio_worklet_output";
    state.audio.lastSampleAtMs = atMs;
    state.audio.lastRms = rms;
    state.audio.lastPeak = peak;
    if (detail.audible) {
      state.audio.currentlyAudible = true;
      state.audio.lastAudibleAtMs = atMs;
      if (state.turn && state.turn.firstNonSilentAtMs === null && (!generation || generation === state.turn.generation)) {
        state.turn.firstNonSilentAtMs = atMs;
        state.turn.firstNonSilentRms = rms;
        state.turn.firstNonSilentPeak = peak;
      } else if (!state.turn) {
        state.audio.pendingFirstNonSilent = { atMs, rms, peak, generation };
      }
      if (
        state.interrupt
        && state.interrupt.sustainedSilenceAtMs !== null
        && state.interrupt.newGenerationAtMs === null
        && state.interrupt.staleAudioAfterSilenceAtMs === null
      ) {
        state.interrupt.staleAudioAfterSilenceAtMs = atMs;
      }
    } else {
      state.audio.currentlyAudible = false;
      if (state.interrupt && state.interrupt.sustainedSilenceAtMs === null) {
        state.interrupt.sustainedSilenceAtMs = atMs;
      }
    }
  };
  window.addEventListener("jixia:agent-audio-audibility", onWorkletAudibility);
  const onNetworkStats = (event) => {
    const detail = JSON.parse(JSON.stringify(event?.detail || {}));
    state.network.last = detail;
    state.network.samples.push(detail);
    if (state.network.samples.length > 60) state.network.samples.shift();
  };
  window.addEventListener("jixia:agent-audio-network", onNetworkStats);
  const onDiagnostic = (event) => {
    const detail = JSON.parse(JSON.stringify(event?.detail || {}));
    state.diagnostics.push(detail);
    if (state.diagnostics.length > 100) state.diagnostics.shift();
  };
  window.addEventListener("jixia:agent-audio-diagnostic", onDiagnostic);

  // Do not replace the WebSocket constructor. LiveKit snapshots the native
  // constructor during module initialization and can stall when an init script
  // swaps it for a subclass. The app's room socket uses `onmessage`, so wrapping
  // that native property is sufficient and leaves WebRTC signaling untouched.
  const webSocketPrototype = window.WebSocket.prototype;
  const nativeOnMessage = Object.getOwnPropertyDescriptor(webSocketPrototype, "onmessage");
  if (nativeOnMessage?.get && nativeOnMessage.set) {
    Object.defineProperty(webSocketPrototype, "onmessage", {
      configurable: nativeOnMessage.configurable,
      enumerable: nativeOnMessage.enumerable,
      get() { return nativeOnMessage.get.call(this); },
      set(handler) {
        const normalizedUrl = String(this.url || "");
        if (!/\/ws\/rooms\/[^/]+(?:\?|$)/.test(normalizedUrl) || typeof handler !== "function") {
          nativeOnMessage.set.call(this, handler);
          return;
        }
        if (!state.socketUrls.includes(normalizedUrl)) {
          state.socketUrls.push(normalizedUrl.replace(/([?&](?:token|access_token)=)[^&]+/gi, "$1[redacted]"));
        }
        nativeOnMessage.set.call(this, (event) => {
          if (typeof event.data === "string") try {
            const message = JSON.parse(event.data);
            if (Array.isArray(message?.room?.recent_events)) {
              for (const item of message.room.recent_events) {
                const seq = Number(item?.seq) || 0;
                if (!seq || seenRoomEventSeq.has(seq) || typeof item?.type !== "string") continue;
                seenRoomEventSeq.add(seq);
                ingestEvent({
                  type: item.type,
                  room_code: message?.room?.code || "",
                  seq,
                  ...(item.payload && typeof item.payload === "object" ? item.payload : {}),
                });
              }
            }
            if (message?.event) {
              const eventPayload = { ...message.event };
              const seq = Number(eventPayload.seq) || 0;
              if (!seq || !seenRoomEventSeq.has(seq)) {
                if (seq) seenRoomEventSeq.add(seq);
                ingestEvent(eventPayload);
              }
            }
          } catch {
            // Non-JSON messages are unrelated to the room event channel.
          }
          return handler.call(this, event);
        });
      },
    });
  }

  const markFlushAction = (action) => {
    const interrupt = state.interrupt;
    if (!interrupt || interrupt.firstFlushActionAtMs !== null) return;
    interrupt.firstFlushActionAtMs = epochNow();
    interrupt.flushAction = action;
  };

  const nativePause = HTMLMediaElement.prototype.pause;
  HTMLMediaElement.prototype.pause = function qaPause() {
    if (this instanceof HTMLAudioElement && this.dataset.jixiaAgentAudio) markFlushAction("pause");
    return nativePause.call(this);
  };
  const nativeLoad = HTMLMediaElement.prototype.load;
  HTMLMediaElement.prototype.load = function qaLoad() {
    if (this instanceof HTMLAudioElement && this.dataset.jixiaAgentAudio) markFlushAction("load");
    return nativeLoad.call(this);
  };

  const ensureContext = async () => {
    if (context && context.state !== "closed") {
      if (context.state !== "running") await context.resume();
      state.audio.contextState = context.state;
      if (context.state === "running" && state.audio.playbackUnlockedAtMs === null) {
        state.audio.playbackUnlockedAtMs = epochNow();
      }
      return context;
    }
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) throw new Error("AudioContext is unavailable");
    context = new AudioContextClass({ latencyHint: "interactive" });
    state.audio.contextState = context.state;
    if (context.state !== "running") await context.resume();
    state.audio.contextState = context.state;
    if (context.state === "running") state.audio.playbackUnlockedAtMs = epochNow();
    return context;
  };

  document.addEventListener("click", () => {
    void ensureContext().catch((error) => recordError("resume-on-click", error));
  }, { capture: true });

  const disconnectGraph = () => {
    try { source?.disconnect(); } catch { /* best effort */ }
    try { analyser?.disconnect(); } catch { /* best effort */ }
    try { silentGain?.disconnect(); } catch { /* best effort */ }
    source = null;
    analyser = null;
    silentGain = null;
    sampleData = null;
    monitoredStream = null;
  };

  const bindAudioStream = async (audio, stream) => {
    if (monitoredAudio === audio && monitoredStream === stream && analyser) return;
    disconnectGraph();
    monitoredAudio = audio;
    monitoredStream = stream;
    const activeContext = await ensureContext();
    source = activeContext.createMediaStreamSource(stream);
    analyser = activeContext.createAnalyser();
    analyser.fftSize = 256;
    analyser.smoothingTimeConstant = 0;
    silentGain = activeContext.createGain();
    silentGain.gain.value = 0;
    source.connect(analyser);
    analyser.connect(silentGain);
    silentGain.connect(activeContext.destination);
    sampleData = new Float32Array(analyser.fftSize);
    state.audio.streamSeenAtMs = epochNow();
  };

  const scanAudioElement = () => {
    const audio = document.querySelector("audio[data-jixia-agent-audio]");
    if (!(audio instanceof HTMLAudioElement)) return;
    if (state.audio.elementSeenAtMs === null) {
      state.audio.elementSeenAtMs = epochNow();
      audio.addEventListener("playing", () => { state.audio.playingAtMs = epochNow(); });
    }
    monitoredAudio = audio;
    const stream = audio.srcObject;
    if (stream instanceof MediaStream && stream.getAudioTracks().length > 0) {
      void bindAudioStream(audio, stream).catch((error) => recordError("bind-media-stream", error));
    } else {
      if (state.interrupt && monitoredStream !== null) {
        markFlushAction("srcObject=null");
        // Detaching the only MediaStream removes the old generation from the
        // browser render graph immediately; unlike an RMS window, this remains
        // observable even after the analyser source itself is disconnected.
        if (state.interrupt.sustainedSilenceAtMs === null) {
          state.interrupt.sustainedSilenceAtMs = epochNow();
        }
        state.audio.currentlyAudible = false;
      }
      if (monitoredStream !== null) disconnectGraph();
    }
  };

  const sample = () => {
    if (disposed) return;
    scanAudioElement();
    const atMs = epochNow();
    if (analyser && sampleData && monitoredAudio) {
      analyser.getFloatTimeDomainData(sampleData);
      let sum = 0;
      let peak = 0;
      for (let index = 0; index < sampleData.length; index += 1) {
        const value = sampleData[index];
        sum += value * value;
        peak = Math.max(peak, Math.abs(value));
      }
      const rms = Math.sqrt(sum / sampleData.length);
      state.audio.lastSampleAtMs = atMs;
      state.audio.lastRms = rms;
      state.audio.lastPeak = peak;
      state.audio.contextState = context?.state || "missing";
      const playbackEligible = context?.state === "running" && !monitoredAudio.muted && !monitoredAudio.paused;
      if (playbackEligible && rms >= state.config.audibleRms) {
        audibleFrames += 1;
        silenceFrames = 0;
      } else if (rms <= state.config.silenceRms || !playbackEligible) {
        silenceFrames += 1;
        audibleFrames = 0;
      } else {
        audibleFrames = 0;
        silenceFrames = 0;
      }

      if (audibleFrames >= state.config.audibleFrames) {
        state.audio.currentlyAudible = true;
        state.audio.lastAudibleAtMs = atMs;
        if (state.turn && state.turn.firstNonSilentAtMs === null) {
          state.turn.firstNonSilentAtMs = atMs;
          state.turn.firstNonSilentRms = rms;
          state.turn.firstNonSilentPeak = peak;
        }
        if (
          state.interrupt
          && state.interrupt.sustainedSilenceAtMs !== null
          && state.interrupt.newGenerationAtMs === null
          && state.interrupt.staleAudioAfterSilenceAtMs === null
        ) {
          state.interrupt.staleAudioAfterSilenceAtMs = atMs;
        }
      }
      if (silenceFrames >= state.config.silenceFrames) {
        state.audio.currentlyAudible = false;
        if (state.interrupt && state.interrupt.sustainedSilenceAtMs === null) {
          state.interrupt.sustainedSilenceAtMs = atMs;
        }
      }
    }
    requestAnimationFrame(sample);
  };
  requestAnimationFrame(sample);

  const report = () => {
    const turn = state.turn;
    const interrupt = state.interrupt;
    const serverCaptureAtMs = turn?.serverFirstCaptureAt ? Date.parse(turn.serverFirstCaptureAt) : Number.NaN;
    const agentDeltaAtMs = turn?.agentFirstReadableDeltaAt ? Date.parse(turn.agentFirstReadableDeltaAt) : Number.NaN;
    return {
      ...JSON.parse(JSON.stringify(state)),
      derived: {
        rtc_event_to_non_silent_ms: turn?.firstNonSilentAtMs != null
          ? Number((turn.firstNonSilentAtMs - turn.eventReceivedAtMs).toFixed(3))
          : null,
        server_capture_to_browser_non_silent_clock_estimate_ms: turn?.firstNonSilentAtMs != null && Number.isFinite(serverCaptureAtMs)
          ? Number((turn.firstNonSilentAtMs - serverCaptureAtMs).toFixed(3))
          : null,
        agent_delta_to_server_capture_ms: Number.isFinite(agentDeltaAtMs) && Number.isFinite(serverCaptureAtMs)
          ? Number((serverCaptureAtMs - agentDeltaAtMs).toFixed(3))
          : null,
        agent_delta_to_browser_non_silent_clock_estimate_ms: turn?.firstNonSilentAtMs != null && Number.isFinite(agentDeltaAtMs)
          ? Number((turn.firstNonSilentAtMs - agentDeltaAtMs).toFixed(3))
          : null,
        interrupt_event_to_flush_ms: interrupt?.firstFlushActionAtMs != null
          ? Number((interrupt.firstFlushActionAtMs - interrupt.eventReceivedAtMs).toFixed(3))
          : null,
        interrupt_event_to_sustained_silence_ms: interrupt?.sustainedSilenceAtMs != null
          ? Number((interrupt.sustainedSilenceAtMs - interrupt.eventReceivedAtMs).toFixed(3))
          : null,
      },
    };
  };

  window.__jixiaRealtimeAudioGate = {
    state,
    configure(options = {}) {
      for (const key of Object.keys(state.config)) {
        if (Number.isFinite(options[key]) && options[key] > 0) state.config[key] = Number(options[key]);
      }
      return report();
    },
    ingestEvent,
    markAgentFirstReadableDelta(value) {
      if (!state.turn) return false;
      const date = value instanceof Date ? value.toISOString() : String(value || "");
      if (!Number.isFinite(Date.parse(date))) return false;
      state.turn.agentFirstReadableDeltaAt = date;
      return true;
    },
    report,
    async resume() {
      await ensureContext();
      return report();
    },
    async dispose() {
      disposed = true;
      window.removeEventListener("jixia:agent-audio-audibility", onWorkletAudibility);
      window.removeEventListener("jixia:agent-audio-network", onNetworkStats);
      window.removeEventListener("jixia:agent-audio-diagnostic", onDiagnostic);
      disconnectGraph();
      if (nativeOnMessage) Object.defineProperty(webSocketPrototype, "onmessage", nativeOnMessage);
      await context?.close().catch(() => undefined);
      context = null;
    },
  };
})();
