/**
 * Merge an ASR update into the text already confirmed by the browser.
 *
 * Streaming providers are inconsistent around reconnects: some resume with
 * only new text, while others resend the entire cumulative hypothesis. A
 * direct concatenation duplicates everything the user said before the
 * reconnect. Keep the longest trustworthy overlap and preserve the provider's
 * newest cumulative value when it already contains the committed prefix.
 */
export function mergeAsrText(committed: string, incoming: string): string {
  const previous = committed.trim();
  const next = incoming.trim();
  if (!previous) return next;
  if (!next) return previous;
  if (next.startsWith(previous)) return next;
  if (previous.endsWith(next)) return previous;

  const previousCharacters = Array.from(previous);
  const nextCharacters = Array.from(next);
  const maximumOverlap = Math.min(previousCharacters.length, nextCharacters.length);
  for (let overlap = maximumOverlap; overlap >= 1; overlap -= 1) {
    const previousSuffix = previousCharacters.slice(-overlap).join("");
    const nextPrefix = nextCharacters.slice(0, overlap).join("");
    if (previousSuffix !== nextPrefix) continue;
    // A one-character overlap is only reliable for repeated punctuation.
    // Ordinary Chinese characters commonly repeat by coincidence across a
    // sentence boundary and must not be silently removed.
    if (overlap === 1 && !/[，。！？；：,.!?;:]/u.test(previousSuffix)) continue;
    return `${previous}${nextCharacters.slice(overlap).join("")}`;
  }
  return `${previous}${next}`;
}
