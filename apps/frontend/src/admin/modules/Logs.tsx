import * as React from "react";
import { ScrollText, RefreshCw, Download, ChevronDown, Bot, AudioLines, ShieldCheck, Filter, Trash2 } from "lucide-react";
import { Button, Card, CardContent, Badge, Input, Select, EmptyState, Switch } from "../ui/primitives";
import { Tabs } from "../ui/Tabs";
import { useToast } from "../lib/toast";
import { useAdminData } from "../lib/data";
import { getRequestLogs, clearRequestLogs } from "../../api/client";
import type { RequestLogs } from "../../types/contracts";

type Kind = "agent" | "speech" | "audit";
interface Row {
  id: string;
  kind: Kind;
  time: string;
  status: string;
  ok: boolean;
  latency: number | null;
  label: string;
  detail: string;
  output: string;
  raw: unknown;
}

function preview(v: unknown, n = 160): string {
  if (v == null) return "";
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s.length > n ? `${s.slice(0, n)}…` : s;
}

function flatten(logs: RequestLogs): Row[] {
  const rows: Row[] = [];
  for (const r of logs.agent_requests) {
    rows.push({
      id: `agent-${r.id}`,
      kind: "agent",
      time: r.started_at,
      status: r.status,
      ok: r.status === "completed" || r.status === "succeeded",
      latency: r.latency_ms,
      label: r.endpoint,
      detail: r.error_message || r.task_id,
      output: r.error_message ? "" : preview(r.response_text),
      raw: r,
    });
  }
  for (const r of logs.speech_service_requests) {
    rows.push({
      id: `speech-${r.id}`,
      kind: "speech",
      time: r.started_at,
      status: r.status,
      ok: r.status === "completed" || r.status === "succeeded",
      latency: r.latency_ms,
      label: `${r.service}.${r.operation}`,
      detail: r.error_message || "",
      output: r.error_message ? "" : preview(r.response),
      raw: r,
    });
  }
  for (const r of logs.audit_logs) {
    rows.push({
      id: `audit-${r.id}`,
      kind: "audit",
      time: r.created_at,
      status: r.result,
      ok: r.result === "ok" || r.result === "success",
      latency: null,
      label: r.action,
      detail: r.target_type || "",
      output: r.error_message || preview(r.request),
      raw: r,
    });
  }
  return rows.sort((a, b) => (a.time < b.time ? 1 : -1));
}

const KIND_META: Record<Kind, { label: string; icon: typeof Bot }> = {
  agent: { label: "Agent", icon: Bot },
  speech: { label: "语音", icon: AudioLines },
  audit: { label: "审计", icon: ShieldCheck },
};

export function Logs() {
  const { matchId } = useAdminData();
  const toast = useToast();
  const [logs, setLogs] = React.useState<RequestLogs | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [kind, setKind] = React.useState<"all" | Kind>("all");
  const [status, setStatus] = React.useState<"all" | "ok" | "failed">("all");
  const [query, setQuery] = React.useState("");
  const [full, setFull] = React.useState(false);
  const [auto, setAuto] = React.useState(false);
  const [expanded, setExpanded] = React.useState<Set<string>>(new Set());

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      setLogs(await getRequestLogs(matchId, 500));
    } catch (err) {
      toast(err instanceof Error ? err.message : "加载失败", "error");
    } finally {
      setLoading(false);
    }
  }, [matchId, toast]);

  React.useEffect(() => {
    void load();
  }, [load]);

  React.useEffect(() => {
    if (!auto) return;
    const t = window.setInterval(load, 5000);
    return () => window.clearInterval(t);
  }, [auto, load]);

  const rows = React.useMemo(() => {
    if (!logs) return [];
    let r = flatten(logs);
    if (kind !== "all") r = r.filter((x) => x.kind === kind);
    if (status !== "all") r = r.filter((x) => (status === "ok" ? x.ok : !x.ok));
    if (query.trim()) {
      const q = query.toLowerCase();
      r = r.filter((x) => `${x.label} ${x.detail} ${JSON.stringify(x.raw)}`.toLowerCase().includes(q));
    }
    return r;
  }, [logs, kind, status, query]);

  async function clear() {
    if (!confirm("确认清空当前比赛的全部请求日志？该操作不可恢复。")) return;
    try {
      setLogs(await clearRequestLogs(matchId));
      toast("日志已清空", "success");
    } catch (err) {
      toast(err instanceof Error ? err.message : "清空失败", "error");
    }
  }

  function download() {
    const blob = new Blob([JSON.stringify(rows.map((r) => r.raw), null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `phdebate-logs-${Date.now()}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  function toggle(id: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Tabs
          value={kind}
          onValueChange={(v) => setKind(v as typeof kind)}
          items={[
            { value: "all", label: "全部" },
            { value: "agent", label: "Agent" },
            { value: "speech", label: "语音" },
            { value: "audit", label: "审计" },
          ]}
        />
        <Select value={status} onChange={(e) => setStatus(e.target.value as typeof status)} className="h-9 w-28">
          <option value="all">全部状态</option>
          <option value="ok">成功</option>
          <option value="failed">失败</option>
        </Select>
        <div className="relative min-w-[180px] flex-1">
          <Filter className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索接口/动作/内容…" className="px-9" />
        </div>
        <label className="flex items-center gap-2 text-sm text-muted-foreground">
          <Switch checked={full} onCheckedChange={setFull} /> 完整
        </label>
        <label className="flex items-center gap-2 text-sm text-muted-foreground">
          <Switch checked={auto} onCheckedChange={setAuto} /> 自动刷新
        </label>
        <Button variant="outline" size="sm" onClick={load} loading={loading}>
          <RefreshCw /> 刷新
        </Button>
        <Button variant="outline" size="sm" onClick={download} disabled={rows.length === 0}>
          <Download /> 下载
        </Button>
        <Button variant="ghost" size="sm" className="text-destructive hover:bg-destructive/10" onClick={clear}>
          <Trash2 /> 清空
        </Button>
      </div>

      <Card>
        <CardContent className="p-0">
          {rows.length === 0 ? (
            <EmptyState icon={<ScrollText />} title="暂无日志" description="当前筛选条件下没有请求记录。" />
          ) : (
            <div className="divide-y divide-border">
              <div className="grid grid-cols-[80px_1fr_90px_70px_28px] items-center gap-2 px-4 py-2 text-xs font-medium text-muted-foreground">
                <span>类型</span>
                <span>接口 / 动作</span>
                <span>状态</span>
                <span>延迟</span>
                <span />
              </div>
              {rows.map((r) => {
                const Icon = KIND_META[r.kind].icon;
                const isOpen = full || expanded.has(r.id);
                return (
                  <div key={r.id}>
                    <button
                      onClick={() => toggle(r.id)}
                      className="grid w-full grid-cols-[80px_1fr_90px_70px_28px] items-center gap-2 px-4 py-2.5 text-left text-sm transition-colors hover:bg-accent"
                    >
                      <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                        <Icon className="size-3.5" /> {KIND_META[r.kind].label}
                      </span>
                      <span className="min-w-0">
                        <span className="block truncate font-mono text-xs text-foreground">{r.label}</span>
                        {r.detail && <span className="block truncate text-xs text-muted-foreground">{r.detail}</span>}
                        {r.output && (
                          <span className="block truncate text-xs text-primary/80">↳ 输出：{r.output}</span>
                        )}
                        <span className="block text-[11px] text-muted-foreground">{new Date(r.time).toLocaleString()}</span>
                      </span>
                      <Badge variant={r.ok ? "success" : "destructive"}>{r.status}</Badge>
                      <span className="text-xs text-muted-foreground">{r.latency != null ? `${r.latency}ms` : "—"}</span>
                      <ChevronDown className={`size-4 text-muted-foreground transition-transform ${isOpen ? "rotate-180" : ""}`} />
                    </button>
                    {isOpen && (
                      <pre className="overflow-x-auto bg-muted/40 px-4 py-3 text-xs text-foreground">
                        {JSON.stringify(r.raw, null, 2)}
                      </pre>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>
      {!full && rows.length > 0 && (
        <p className="text-center text-xs text-muted-foreground">共 {rows.length} 条 · 点击任意行展开完整请求/返回</p>
      )}
    </div>
  );
}
