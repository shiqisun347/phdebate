import type { Competition } from "@/lib/types";

export const PRIMARY_COMPETITION_SLUG = "daily-4v4";
export const PRIMARY_COMPETITION_NAME = "4v4 人机辩论正式赛";
const LEGACY_PRIMARY_COMPETITION_NAME = "4v4 人机辩论日常赛";

export function isPrimaryCompetition(competition: Pick<Competition, "slug" | "format" | "ranked">): boolean {
  return competition.slug === PRIMARY_COMPETITION_SLUG
    || (competition.format === "4v4" && competition.ranked);
}

export function competitionDisplayName(competition: Pick<Competition, "slug" | "format" | "ranked" | "name">): string {
  return isPrimaryCompetition(competition) ? PRIMARY_COMPETITION_NAME : competition.name;
}

export function competitionDisplayNameFromStoredName(name: string): string {
  return name === LEGACY_PRIMARY_COMPETITION_NAME ? PRIMARY_COMPETITION_NAME : name;
}
