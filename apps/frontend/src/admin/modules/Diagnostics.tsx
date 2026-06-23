import * as React from "react";
import { Stethoscope, RefreshCw, CheckCircle2, AlertTriangle, XCircle, Bot, Mic, Volume2, Monitor, Activity } from "lucide-react";
import { Button, Card, CardContent, CardHeader, CardTitle, Badge, Spinner, Separator } from "../ui/primitives";
import { useToast } from "../lib/toast";
import { useAdminData } from "../lib/data";
import { getPreflightReport, post } from "../../api/client";
import type { PreflightReport, PreflightStatus } from "../../types/contracts";

function statusIcon(s: PreflightStatus) {
  if (s === "ok") return <CheckCircle2 className="size-4 text-success" />;
  if (s === "warn") return <AlertTriangle className="size-4 text-warning" />;
  return <XCircle className="size-4 text-destructive" />;
}
function statusVariant(s: PreflightStatus): "success" | "warning" | "destructive" {
  return s === "ok" ? "success" : s === "warn" ? "warning" : "destructive";
}

export function Diagnostics() {
  const { snapshot, matchId, refresh } = useAdminData();
  const toast = useToast();
  const [report, setReport] = React.useState<PreflightReport | null>(null);
  const [loading, setLoading] = React.useState(false);

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      setReport(await getPreflightReport(matchId));
    } catch (err) {
      toast(err instanceof Error ? err.message : "加载失败", "error");
    } finally {
      setLoading(false);
    }
  }, [matchId, toast]);

  React.useEffect(() => {
    void load();
  }, [load]);

  async function healthCheck() {
    try {
      await post(`/api/matches/${matchId}/agents/health`);
      await Promise.all([refresh(), load()]);
      toast("已发起 Agent 健康检查", "success");
    } catch (err) {
      toast(err instanceof Error ? err.message : "检查失败", "error");
    }
  }

  const ss = snapshot?.speech_service;
  const agentStatus = snapshot?.agent_status ?? [];

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">赛前设备与功能自检。与比赛过程检测无关，用于确保现场不出问题。</p>
        <div className="flex gap-2">
          <Button variant="outline" onClick={healthCheck}>
            <Activity /> Agent 健康检查
          </Button>
          <Button onClick={load} loading={loading}>
            <RefreshCw /> 重新自检
          </Button>
        </div>
      </div>

      {report && (
        <Card>
          <CardContent className="flex flex-wrap items-center gap-3 p-4">
            {statusIcon(report.overall_status)}
            <span className="text-sm font-medium text-foreground">{report.summary}</span>
            <div className="ml-auto flex gap-1.5">
              <Badge variant="success">通过 {report.score.ok}</Badge>
              <Badge variant="warning">警告 {report.score.warn}</Badge>
              <Badge variant="destructive">失败 {report.score.fail}</Badge>
            </div>
          </CardContent>
        </Card>
      )}

      {/* 设备/服务总览 */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <DeviceTile icon={<Mic />} label="ASR 识别" status={ss?.asr.status} detail={`${ss?.asr.latency_ms ?? "—"}ms`} />
        <DeviceTile icon={<Volume2 />} label="TTS 合成" status={ss?.tts.status} detail={`${ss?.tts.latency_ms ?? "—"}ms`} />
        <DeviceTile icon={<Monitor />} label="大屏" status={ss?.screen.status} detail="screen" />
        <DeviceTile
          icon={<Bot />}
          label="辩手控制台"
          status={ss ? (ss.consoles.online === ss.consoles.total ? "ready" : "partial") : undefined}
          detail={ss ? `${ss.consoles.online}/${ss.consoles.total} 在线` : "—"}
        />
      </div>

      {/* Agent 状态 */}
      {agentStatus.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">AI 辩手状态</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-2 sm:grid-cols-2">
            {agentStatus.map((a) => (
              <div key={a.speaker_id} className="flex items-center justify-between rounded-md border border-border p-3">
                <div className="flex items-center gap-2">
                  <Bot className="size-4 text-primary" />
                  <div>
                    <p className="text-sm font-medium text-foreground">{a.name}</p>
                    <p className="text-xs text-muted-foreground">{a.model} · {a.detail}</p>
                  </div>
                </div>
                <Badge variant={a.status === "ready" || a.status === "speech_only" ? "success" : a.status === "failed" ? "destructive" : "warning"}>
                  {a.status === "speech_only" ? "ready" : a.status}
                </Badge>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      {/* preflight 分区明细 */}
      {report?.sections.map((sec) => (
        <Card key={sec.id}>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-sm">
              {statusIcon(sec.status)} {sec.label}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {sec.checks.map((c) => (
              <div key={c.id} className="flex items-start justify-between gap-3 rounded-md border border-border p-2.5">
                <div className="flex items-start gap-2">
                  {statusIcon(c.status)}
                  <div>
                    <p className="text-sm text-foreground">{c.label}</p>
                    <p className="text-xs text-muted-foreground">{c.detail}</p>
                  </div>
                </div>
                <Badge variant={statusVariant(c.status)}>{c.status}</Badge>
              </div>
            ))}
          </CardContent>
        </Card>
      ))}

      {report && report.next_actions.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">建议操作</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="space-y-1">
              {report.next_actions.map((a, i) => (
                <li key={i} className="flex items-start gap-2 text-sm text-muted-foreground">
                  <Separator className="mt-2 w-3 shrink-0" /> {a}
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function DeviceTile({ icon, label, status, detail }: { icon: React.ReactNode; label: string; status?: string; detail: string }) {
  const ok = status === "ready" || status === "ok";
  const warn = status === "partial" || status === "degraded" || status === "mock";
  return (
    <Card>
      <CardContent className="flex items-center gap-3 p-4">
        <div className={`flex size-10 items-center justify-center rounded-lg [&_svg]:size-5 ${ok ? "bg-success/12 text-success" : warn ? "bg-warning/15 text-warning" : "bg-destructive/12 text-destructive"}`}>
          {icon}
        </div>
        <div className="min-w-0">
          <p className="text-sm font-medium text-foreground">{label}</p>
          <p className="truncate text-xs text-muted-foreground">{status ?? "未知"} · {detail}</p>
        </div>
      </CardContent>
    </Card>
  );
}
