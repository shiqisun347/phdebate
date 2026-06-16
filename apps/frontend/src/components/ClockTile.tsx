import type { Clock } from "../types/contracts";
import { clockStateLabel, formatMs } from "../state/format";
import { useClockRemaining } from "../hooks/useClockRemaining";

interface ClockTileProps {
  label: string;
  clock?: Clock;
  tone?: "aff" | "neg" | "turn" | "main";
  compact?: boolean;
}

export function ClockTile({ label, clock, tone = "main", compact = false }: ClockTileProps) {
  const remaining = useClockRemaining(clock);
  const ratio = clock ? Math.max(0, Math.min(1, remaining / (clock.total_seconds * 1000))) : 0;

  return (
    <div className={`clock-tile ${tone} ${compact ? "compact" : ""}`}>
      <div className="clock-label">{label}</div>
      <div className="clock-value">{tone === "turn" ? Math.ceil(remaining / 1000) : formatMs(remaining)}</div>
      <div className="clock-bar">
        <i style={{ width: `${ratio * 100}%` }} />
      </div>
      <div className="clock-state">{clockStateLabel(clock?.state)}</div>
    </div>
  );
}
