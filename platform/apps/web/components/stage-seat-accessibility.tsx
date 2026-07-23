"use client";

import { useLayoutEffect } from "react";

import type { Room } from "@/lib/types";

const MANAGED_ATTRIBUTE = "data-stage-seat-a11y";
const CURRENT_SPEAKER_LABEL = "当前发言席位";
const TERMINAL_ROOM_STATUSES = new Set(["completed", "review_required", "terminated", "cancelled"]);

/**
 * Corrects the frozen stage's screen-reader wording without changing its
 * visual state or audio/playback implementation. During an idle free-debate
 * turn the highlighted seats are eligible speakers, not simultaneous current
 * speakers.
 */
export function StageSeatAccessibility({ room }: { room: Room }) {
  useLayoutEffect(() => {
    if (
      TERMINAL_ROOM_STATUSES.has(room.status)
      || room.current_stage?.kind !== "free"
      || !room.current_stage.side
      || room.active_speech
    ) return;

    const side = room.current_stage.side;
    const paused = room.status === "paused";
    const seatLabel = paused
      ? "本轮可发言席位，比赛已暂停"
      : "本轮可发言席位";
    const sideLabel = paused
      ? "本轮轮到的阵营，比赛已暂停"
      : "当前可发言阵营";
    const managedSources: HTMLElement[] = [];
    const replacementHints: HTMLElement[] = [];

    document
      .querySelectorAll<HTMLElement>(`.stage-page .team-column.${side} .stage-seat.active .sr-only`)
      .forEach((node) => {
        if (!node.textContent?.includes(CURRENT_SPEAKER_LABEL)) return;
        managedSources.push(node);
        node.setAttribute("aria-hidden", "true");
        node.setAttribute(MANAGED_ATTRIBUTE, "source");

        const replacement = document.createElement("span");
        replacement.className = "sr-only";
        replacement.textContent = node.textContent.replace(CURRENT_SPEAKER_LABEL, seatLabel);
        replacement.setAttribute(MANAGED_ATTRIBUTE, "seat");
        node.after(replacement);
        replacementHints.push(replacement);
      });

    const teamTitle = document.querySelector<HTMLElement>(`.stage-page .team-column.${side} .team-title`);
    const titleHint = document.createElement("span");
    if (teamTitle) {
      titleHint.className = "sr-only";
      titleHint.textContent = ` · ${sideLabel}`;
      titleHint.setAttribute(MANAGED_ATTRIBUTE, "side");
      teamTitle.append(titleHint);
    }

    return () => {
      managedSources.forEach((node) => {
        if (node.getAttribute(MANAGED_ATTRIBUTE) !== "source") return;
        node.removeAttribute("aria-hidden");
        node.removeAttribute(MANAGED_ATTRIBUTE);
      });
      replacementHints.forEach((node) => node.remove());
      titleHint.remove();
    };
  }, [room.active_speech, room.current_stage?.kind, room.current_stage?.side, room.seq, room.status]);

  return null;
}
