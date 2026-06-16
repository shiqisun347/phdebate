import { Flag, FlagTriangleRight, Clock } from "lucide-react";
import { cn } from "../lib/cn";
import { sideLabel } from "../lib/labels";
import type { RulesetFlowNode } from "../../types/contracts";

function sideClasses(side: string) {
  if (side === "affirmative") return "border-l-4 border-l-blue-500 bg-blue-500/5";
  if (side === "negative") return "border-l-4 border-l-rose-500 bg-rose-500/5";
  return "border-l-4 border-l-muted-foreground bg-muted/40";
}

function fmt(sec: number) {
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return m ? `${m}分${s ? `${s}秒` : ""}` : `${s}秒`;
}

export function FlowDiagram({ nodes, compact }: { nodes: RulesetFlowNode[]; compact?: boolean }) {
  if (!nodes?.length) {
    return <p className="py-6 text-center text-sm text-muted-foreground">暂无流程环节</p>;
  }
  return (
    <div className="relative">
      <div className="mb-2 flex items-center gap-2 text-sm font-medium text-success">
        <Flag className="size-4" /> 比赛开始
      </div>
      <ol className="space-y-2 border-l border-dashed border-border pl-4">
        {nodes.map((n, i) => (
          <li key={n.key || i} className="relative">
            <span className="absolute -left-[1.42rem] top-3 size-2.5 rounded-full bg-primary ring-4 ring-background" />
            <div className={cn("flex items-center justify-between gap-3 rounded-md border border-border p-3", sideClasses(n.side))}>
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-foreground">
                  <span className="mr-1.5 text-xs text-muted-foreground">#{i + 1}</span>
                  {n.name}
                </p>
                {!compact && (
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    {sideLabel(n.side)} · {n.speaker || "—"} · {n.phase_type}
                  </p>
                )}
              </div>
              <span className="flex shrink-0 items-center gap-1 rounded-full bg-card px-2 py-0.5 text-xs font-medium text-foreground">
                <Clock className="size-3" /> {fmt(n.duration_seconds)}
              </span>
            </div>
          </li>
        ))}
      </ol>
      <div className="mt-2 flex items-center gap-2 text-sm font-medium text-muted-foreground">
        <FlagTriangleRight className="size-4" /> 比赛结束
      </div>
    </div>
  );
}
