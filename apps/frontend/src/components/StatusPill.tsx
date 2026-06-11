import type { ReactNode } from "react";

interface StatusPillProps {
  tone?: "green" | "blue" | "red" | "gold" | "muted";
  children: ReactNode;
}

export function StatusPill({ tone = "muted", children }: StatusPillProps) {
  return <span className={`status-pill ${tone}`}>{children}</span>;
}
