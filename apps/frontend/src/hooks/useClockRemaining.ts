import { useEffect, useState } from "react";
import type { Clock } from "../types/contracts";
import { clockRemainingAt } from "../state/format";

export function useClockRemaining(clock?: Clock): number {
  const [now, setNow] = useState(() => Date.now());
  const running = clock?.state === "running" && Boolean(clock.deadline_at);

  useEffect(() => {
    if (!running) {
      setNow(Date.now());
      return;
    }
    const tick = () => setNow(Date.now());
    tick();
    const interval = window.setInterval(tick, 100);
    return () => window.clearInterval(interval);
  }, [clock?.deadline_at, running]);

  return clockRemainingAt(clock, now);
}
