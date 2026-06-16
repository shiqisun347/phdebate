import { Trophy, Users, Bot, ListChecks, ArrowRight, Activity } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, Badge, Spinner } from "../ui/primitives";
import { useAdminData } from "../lib/data";
import { STATUS_LABELS, SCENE_LABELS } from "../lib/labels";
import type { ModuleId } from "../nav";

function go(id: ModuleId) {
  window.location.hash = `/${id}`;
}

export function Overview() {
  const { snapshot, matchList } = useAdminData();

  if (!snapshot) {
    return (
      <div className="flex items-center gap-2 py-20 text-muted-foreground">
        <Spinner /> 正在加载当前比赛…
      </div>
    );
  }

  const m = snapshot.match;
  const agents = snapshot.speakers.filter((s) => s.speaker_type === "agent");
  const humans = snapshot.speakers.filter((s) => s.speaker_type === "human");
  const stats = [
    { icon: Users, label: "辩手", value: snapshot.speakers.length, sub: `${humans.length} 人类 · ${agents.length} AI` },
    { icon: Bot, label: "Agent 配置", value: snapshot.agent_configs.length, sub: "已接入" },
    { icon: ListChecks, label: "流程环节", value: snapshot.phases.length, sub: "预设阶段" },
    { icon: Trophy, label: "比赛总数", value: matchList?.matches.length ?? "—", sub: "历史 + 当前" },
  ];

  return (
    <div className="space-y-6">
      <Card className="overflow-hidden">
        <div className="bg-gradient-to-r from-primary/10 via-primary/5 to-transparent p-6">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <Badge variant="success">{STATUS_LABELS[m.status] ?? m.status}</Badge>
                <Badge variant="muted">大屏：{SCENE_LABELS[m.screen_scene] ?? m.screen_scene}</Badge>
              </div>
              <h2 className="text-2xl font-bold text-foreground">{m.title}</h2>
              <p className="max-w-2xl text-sm text-muted-foreground">辩题：{m.topic || "未设置"}</p>
              <p className="text-xs text-muted-foreground">
                {m.organizer && <span>主办：{m.organizer} · </span>}
                正方 {m.affirmative_position || "正方"} vs 反方 {m.negative_position || "反方"}
              </p>
            </div>
            <button
              onClick={() => go("control")}
              className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
            >
              <Activity className="size-4" /> 进入控场台 <ArrowRight className="size-4" />
            </button>
          </div>
        </div>
      </Card>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {stats.map((s) => {
          const Icon = s.icon;
          return (
            <Card key={s.label}>
              <CardContent className="flex items-center gap-4 p-5">
                <div className="flex size-11 items-center justify-center rounded-lg bg-primary/10 text-primary">
                  <Icon className="size-5" />
                </div>
                <div>
                  <p className="text-2xl font-bold leading-none text-foreground">{s.value}</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {s.label} · {s.sub}
                  </p>
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>快捷入口</CardTitle>
          </CardHeader>
          <CardContent className="grid grid-cols-2 gap-2">
            {(
              [
                ["matches", "比赛管理"],
                ["agents", "Agent 管理"],
                ["speech", "语音引擎"],
                ["diagnostics", "赛前调试"],
                ["debaters", "辩手管理"],
                ["flow", "比赛流程"],
              ] as Array<[ModuleId, string]>
            ).map(([id, label]) => (
              <button
                key={id}
                onClick={() => go(id)}
                className="flex items-center justify-between rounded-md border border-border bg-card px-3 py-2.5 text-sm font-medium text-foreground transition-colors hover:border-primary/40 hover:bg-accent"
              >
                {label}
                <ArrowRight className="size-3.5 text-muted-foreground" />
              </button>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>所有比赛</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {matchList?.matches.slice(0, 6).map((mm) => (
              <div
                key={mm.id}
                className="flex items-center justify-between rounded-md border border-border px-3 py-2 text-sm"
              >
                <span className="truncate font-medium text-foreground">{mm.title || mm.id}</span>
                <div className="flex items-center gap-2">
                  {mm.active && <Badge variant="success">当前</Badge>}
                  <Badge variant="muted">{STATUS_LABELS[mm.status] ?? mm.status}</Badge>
                </div>
              </div>
            )) ?? <Spinner />}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
