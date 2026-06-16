import { Workflow, Clock, Lock } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, Badge, Spinner } from "../ui/primitives";
import { FlowDiagram } from "../ui/FlowDiagram";
import { useAdminData } from "../lib/data";
import type { Phase, RulesetFlowNode } from "../../types/contracts";

const SEAT_LABEL = ["", "一辩", "二辩", "三辩", "四辩"];

function phaseToNode(p: Phase): RulesetFlowNode {
  let speaker = "";
  if (p.phase_type === "free_debate") speaker = "双方交替";
  else if (p.speaker_seat) speaker = SEAT_LABEL[p.speaker_seat] ?? `${p.speaker_seat}辩`;
  return {
    key: p.id,
    name: p.name,
    side: p.side,
    speaker,
    duration_seconds: p.duration_seconds,
    phase_type: p.phase_type,
  };
}

export function Flow() {
  const { snapshot } = useAdminData();
  if (!snapshot) {
    return (
      <div className="flex items-center gap-2 py-20 text-muted-foreground">
        <Spinner /> 正在加载比赛流程…
      </div>
    );
  }

  const phases = [...snapshot.phases].sort((a, b) => a.display_order - b.display_order);
  const nodes = phases.map(phaseToNode);
  const totalSec = phases.reduce((sum, p) => sum + p.duration_seconds, 0);
  const rulesetName = (snapshot.match as unknown as { ruleset_name?: string }).ruleset_name;

  return (
    <div className="space-y-5">
      <Card>
        <CardContent className="flex flex-wrap items-center gap-3 p-4">
          <Badge variant="secondary" className="gap-1">
            <Workflow className="size-3.5" /> {rulesetName || "当前赛制"}
          </Badge>
          <Badge variant="muted">{phases.length} 个环节</Badge>
          <Badge variant="muted" className="gap-1">
            <Clock className="size-3.5" /> 总时长约 {Math.round(totalSec / 60)} 分钟
          </Badge>
          <span className="ml-auto flex items-center gap-1 text-xs text-muted-foreground">
            <Lock className="size-3.5" /> 流程为预设，不可自定义
          </span>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>比赛流程图</CardTitle>
        </CardHeader>
        <CardContent>
          <FlowDiagram nodes={nodes} />
        </CardContent>
      </Card>
    </div>
  );
}
