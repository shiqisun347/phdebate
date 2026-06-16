/* Speaker / 小七 avatar resolution.
 *
 * Speakers gained an `image_url` field (uploaded via 辩手管理). When empty we
 * synthesise a stylised default 形象 as an inline SVG data URI so the big
 * screen always has something to show — AI seats get a robot mark, humans get
 * a person silhouette, tinted by side. */
import type { Side, Speaker, SpeakerType } from "../types/contracts";

const SIDE_COLORS: Record<string, { base: string; glow: string; ink: string }> = {
  affirmative: { base: "#2563eb", glow: "#60a5fa", ink: "#dbeafe" },
  negative: { base: "#e11d48", glow: "#fb7185", ink: "#ffe4e6" },
  neutral: { base: "#7c3aed", glow: "#a78bfa", ink: "#ede9fe" },
};

function palette(side: Side | string | undefined) {
  return SIDE_COLORS[side ?? "neutral"] ?? SIDE_COLORS.neutral;
}

function escapeText(value: string): string {
  return value.replace(/[<>&]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c] ?? c));
}

function aiSvg(base: string, glow: string, ink: string, initial: string): string {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
  <defs>
    <radialGradient id="g" cx="50%" cy="38%" r="70%">
      <stop offset="0%" stop-color="${glow}"/>
      <stop offset="100%" stop-color="${base}"/>
    </radialGradient>
  </defs>
  <rect width="200" height="200" fill="#0b1220"/>
  <circle cx="100" cy="96" r="78" fill="url(#g)" opacity="0.18"/>
  <rect x="58" y="60" width="84" height="78" rx="22" fill="url(#g)"/>
  <rect x="58" y="60" width="84" height="78" rx="22" fill="none" stroke="${ink}" stroke-opacity="0.5" stroke-width="2"/>
  <line x1="100" y1="40" x2="100" y2="60" stroke="${ink}" stroke-width="4" stroke-linecap="round"/>
  <circle cx="100" cy="36" r="6" fill="${ink}"/>
  <circle cx="84" cy="92" r="9" fill="#0b1220"/>
  <circle cx="116" cy="92" r="9" fill="#0b1220"/>
  <circle cx="84" cy="92" r="4" fill="${ink}"/>
  <circle cx="116" cy="92" r="4" fill="${ink}"/>
  <rect x="80" y="116" width="40" height="8" rx="4" fill="#0b1220" opacity="0.55"/>
  <text x="100" y="172" text-anchor="middle" font-family="system-ui,Segoe UI,sans-serif" font-size="26" font-weight="700" fill="${ink}">${escapeText(initial)}</text>
</svg>`;
}

function humanSvg(base: string, glow: string, ink: string, initial: string): string {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
  <defs>
    <linearGradient id="h" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${glow}"/>
      <stop offset="100%" stop-color="${base}"/>
    </linearGradient>
  </defs>
  <rect width="200" height="200" fill="#0b1220"/>
  <circle cx="100" cy="100" r="86" fill="url(#h)" opacity="0.16"/>
  <circle cx="100" cy="80" r="34" fill="url(#h)"/>
  <path d="M40 168 a60 60 0 0 1 120 0 z" fill="url(#h)"/>
  <text x="100" y="120" text-anchor="middle" font-family="system-ui,Segoe UI,sans-serif" font-size="30" font-weight="700" fill="${ink}">${escapeText(initial)}</text>
</svg>`;
}

export function defaultAvatarSvg(speakerType: SpeakerType | string, side: Side | string | undefined, name: string): string {
  const { base, glow, ink } = palette(side);
  const initial = (name || "").trim().slice(0, 1) || (speakerType === "agent" ? "AI" : "·");
  return speakerType === "agent" ? aiSvg(base, glow, ink, initial) : humanSvg(base, glow, ink, initial);
}

export function defaultAvatarDataUri(speakerType: SpeakerType | string, side: Side | string | undefined, name: string): string {
  return `data:image/svg+xml;utf8,${encodeURIComponent(defaultAvatarSvg(speakerType, side, name))}`;
}

/** Resolve the image to show for a speaker: uploaded image_url, else a generated default. */
export function resolveAvatar(speaker: Pick<Speaker, "image_url" | "speaker_type" | "side" | "name">): string {
  const url = (speaker.image_url ?? "").trim();
  if (url) return url;
  return defaultAvatarDataUri(speaker.speaker_type, speaker.side, speaker.name);
}
