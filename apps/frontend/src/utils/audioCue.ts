interface BrowserWindowWithAudioContext extends Window {
  webkitAudioContext?: typeof AudioContext;
}

export function playBellCue(durationMs = 800): void {
  const contextWindow = window as BrowserWindowWithAudioContext;
  const AudioContextCtor = window.AudioContext ?? contextWindow.webkitAudioContext;
  if (!AudioContextCtor) return;

  const context = new AudioContextCtor();
  const oscillator = context.createOscillator();
  const gain = context.createGain();
  const durationSeconds = Math.max(0.25, Math.min(2.5, durationMs / 1000));

  oscillator.type = "sine";
  oscillator.frequency.value = 880;
  gain.gain.setValueAtTime(0.0001, context.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.24, context.currentTime + 0.02);
  gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + durationSeconds);
  oscillator.connect(gain).connect(context.destination);
  void context.resume().catch(() => undefined);
  oscillator.start();
  oscillator.stop(context.currentTime + durationSeconds + 0.02);
  oscillator.onended = () => void context.close();
}
