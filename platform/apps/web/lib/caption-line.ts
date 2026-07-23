const CLAUSE_PUNCTUATION = /[，。！？；：,.!?;:]/u;
const CLAUSE_PATTERN = /[^，。！？；：,.!?;:]+[，。！？；：,.!?;:]?/gu;

/**
 * Build the one-line presentation caption shown on the live stage.
 *
 * ASR providers commonly return a cumulative hypothesis. Rendering that
 * value verbatim turns a realtime caption into an ever-growing transcript.
 * The durable transcript is kept elsewhere; this helper deliberately keeps
 * only the newest readable clause and bounds it for a television-style line.
 */
export function compactCaptionLine(value: string, maximumCharacters = 28): string {
  const normalized = value.replace(/\s+/gu, " ").trim();
  if (!normalized) return "";
  const limit = Math.max(8, Math.floor(maximumCharacters));
  const clauses = normalized.match(CLAUSE_PATTERN)?.map((item) => item.trim()).filter(Boolean) || [normalized];
  let selected = clauses.at(-1) || normalized;

  // Very short trailing fragments such as a provider's standalone punctuation
  // are easier to follow when joined to the preceding clause, as long as the
  // result still fits the one-line budget.
  if (clauses.length > 1 && Array.from(selected).length < 6) {
    const previous = clauses.at(-2) || "";
    if (Array.from(previous + selected).length <= limit) selected = previous + selected;
  }

  const characters = Array.from(selected);
  if (characters.length <= limit) return selected;
  const clipped = characters.slice(-(limit - 1)).join("");
  // Do not retain an orphan delimiter immediately after the leading ellipsis.
  return `…${CLAUSE_PUNCTUATION.test(clipped[0] || "") ? clipped.slice(1) : clipped}`;
}
