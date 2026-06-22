import * as React from "react";
import { ScrollText, RefreshCw, Download, ChevronDown, Bot, AudioLines, ShieldCheck, Filter, Trash2, Sparkles } from "lucide-react";
import { Button, Card, CardContent, Badge, Input, Select, EmptyState, Switch } from "../ui/primitives";
import { Tabs } from "../ui/Tabs";
import { useToast } from "../lib/toast";
import { useAdminData } from "../lib/data";
import { getRequestLogs, getRequestLogDetail, clearRequestLogs } from "../../api/client";
import { SCENE_LABELS } from "../lib/labels";
import type { RequestLogDetail, RequestLogKind, RequestLogs, LogOrigin } from "../../types/contracts";

type Kind = "agent" | "speech" | "xiaoqi" | "audit";
interface Row {
  id: string;
  logId: string;
  kind: Kind;
  detailKind: RequestLogKind;
  origin: LogOrigin;
  phase: string;
  scene: string;
  time: string;
  status: string;
  ok: boolean;
  latency: number | null;
  label: string;
  detail: string;
  requestPreview: string;
  outputPreview: string;
  input: unknown;
  output: unknown;
  raw: unknown;
}

function preview(v: unknown, n = 160): string {
  if (v == null) return "";
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s.length > n ? `${s.slice(0, n)}…` : s;
}

function originOf(v: unknown): LogOrigin {
  return ((v as { origin?: string })?.origin as LogOrigin) || "live";
}

function detailInput(detail: RequestLogDetail | undefined): unknown {
  return detail && "request" in detail ? detail.request : null;
}

function detailOutput(detail: RequestLogDetail | undefined): unknown {
  if (!detail) return null;
  if ("response_text" in detail) return detail.error_message || detail.response_text;
  if ("response" in detail) return detail.error_message || detail.response;
  return detail.error_message || "";
}

function flatten(logs: RequestLogs, details: Record<string, RequestLogDetail>): Row[] {
  const rows: Row[] = [];
  for (const r of logs.agent_requests) {
    const rowId = `agent-${r.id}`;
    const detail = details[rowId];
    rows.push({
      id: rowId,
      logId: r.id,
      kind: "agent",
      detailKind: "agent",
      origin: originOf(r),
      phase: r.phase_name || "",
      scene: r.screen_scene || "",
      time: r.started_at,
      status: r.status,
      ok: r.status === "completed" || r.status === "succeeded",
      latency: r.latency_ms,
      label: r.endpoint,
      detail: r.error_message || r.task_id,
      requestPreview: r.request_preview || "",
      outputPreview: r.error_message || r.response_preview || "",
      input: detailInput(detail),
      output: detailOutput(detail),
      raw: detail || r,
    });
  }
  for (const r of logs.speech_service_requests) {
    const isXiaoqi = r.service === "xiaoqi";
    const rowId = `speech-${r.id}`;
    const detail = details[rowId];
    rows.push({
      id: rowId,
      logId: r.id,
      kind: isXiaoqi ? "xiaoqi" : "speech",
      detailKind: isXiaoqi ? "xiaoqi" : "speech",
      origin: originOf(r),
      phase: r.phase_name || "",
      scene: r.screen_scene || "",
      time: r.started_at,
      status: r.status,
      ok: r.status === "completed" || r.status === "succeeded",
      latency: r.latency_ms,
      label: `${r.service}.${r.operation}`,
      detail: r.error_message || "",
      requestPreview: r.request_preview || "",
      outputPreview: r.error_message || r.response_preview || "",
      input: detailInput(detail),
      output: detailOutput(detail),
      raw: detail || r,
    });
  }
  for (const r of logs.audit_logs) {
    const rowId = `audit-${r.id}`;
    const detail = details[rowId];
    rows.push({
      id: rowId,
      logId: r.id,
      kind: "audit",
      detailKind: "audit",
      origin: originOf(r),
      phase: r.phase_name || "",
      scene: r.screen_scene || "",
      time: r.created_at,
      status: r.result,
      ok: r.result === "ok" || r.result === "success",
      latency: null,
      label: r.action,
      detail: r.target_type || "",
      requestPreview: r.request_preview || "",
      outputPreview: r.error_message || "",
      input: detailInput(detail),
      output: detailOutput(detail),
      raw: detail || r,
    });
  }
  return rows.sort((a, b) => (a.time < b.time ? 1 : -1));
}

const KIND_META: Record<Kind, { label: string; icon: typeof Bot }> = {
  agent: { label: "Agent", icon: Bot },
  speech: { label: "语音", icon: AudioLines },
  xiaoqi: { label: "小七", icon: Sparkles },
  audit: { label: "审计", icon: ShieldCheck },
};

function originLabel(origin: LogOrigin): string {
  return origin === "test" ? "测试" : "正式";
}

export function Logs() {
  const { matchId } = useAdminData();
  const toast = useToast();
  const [logs, setLogs] = React.useState<RequestLogs | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [details, setDetails] = React.useState<Record<string, RequestLogDetail>>({});
  const [loadingDetails, setLoadingDetails] = React.useState<Set<string>>(new Set());
  const [kind, setKind] = React.useState<"all" | Kind>("all");
  const [origin, setOrigin] = React.useState<"all" | LogOrigin>("all");
  const [status, setStatus] = React.useState<"all" | "ok" | "failed">("all");
  const [phase, setPhase] = React.useState<string>("all");
  const [query, setQuery] = React.useState("");
  const [full, setFull] = React.useState(false);
  const [auto, setAuto] = React.useState(false);
  const [expanded, setExpanded] = React.useState<Set<string>>(new Set());

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      setLogs(await getRequestLogs(matchId, 1000));
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

  const allRows = React.useMemo(() => (logs ? flatten(logs, details) : []), [logs, details]);
  const phases = React.useMemo(() => Array.from(new Set(allRows.map((r) => r.phase).filter(Boolean))), [allRows]);

  const rows = React.useMemo(() => {
    let r = allRows;
    if (kind !== "all") r = r.filter((x) => x.kind === kind);
    if (origin !== "all") r = r.filter((x) => x.origin === origin);
    if (status !== "all") r = r.filter((x) => (status === "ok" ? x.ok : !x.ok));
    if (phase !== "all") r = r.filter((x) => x.phase === phase);
    if (query.trim()) {
      const q = query.toLowerCase();
      r = r.filter((x) => `${x.label} ${x.detail} ${x.requestPreview} ${x.outputPreview} ${JSON.stringify(x.raw)}`.toLowerCase().includes(q));
    }
    return r;
  }, [allRows, kind, origin, status, phase, query]);

  const ensureDetail = React.useCallback(async (row: Row) => {
    if (details[row.id] || loadingDetails.has(row.id)) return;
    setLoadingDetails((prev) => new Set(prev).add(row.id));
    try {
      const detail = await getRequestLogDetail(matchId, row.detailKind, row.logId);
      setDetails((prev) => ({ ...prev, [row.id]: detail }));
    } catch (err) {
      toast(err instanceof Error ? err.message : "详情加载失败", "error");
    } finally {
      setLoadingDetails((prev) => {
        const next = new Set(prev);
        next.delete(row.id);
        return next;
      });
    }
  }, [details, loadingDetails, matchId, toast]);

  React.useEffect(() => {
    if (!full) return;
    rows
      .filter((row) => !details[row.id] && !loadingDetails.has(row.id))
      .slice(0, 25)
      .forEach((row) => void ensureDetail(row));
  }, [details, ensureDetail, full, loadingDetails, rows]);

  async function clear() {
    if (!confirm("确认清空当前比赛的全部日志？该操作不可恢复。")) return;
    try {
      setLogs(await clearRequestLogs(matchId));
      setDetails({});
      setExpanded(new Set());
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
    a.download = `phdebate-logs-${matchId}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  function toggle(row: Row) {
    const shouldOpen = !expanded.has(row.id);
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(row.id) ? next.delete(row.id) : next.add(row.id);
      return next;
    });
    if (shouldOpen) void ensureDetail(row);
  }

  return (
    <div className="space-y-4">
      {/* 第 2 级 · 类型 */}
      <div className="flex flex-wrap items-center gap-2">
        <Tabs
          value={kind}
          onValueChange={(v) => setKind(v as typeof kind)}
          items={[
            { value: "all", label: "全部" },
            { value: "agent", label: "Agent" },
            { value: "speech", label: "语音" },
            { value: "xiaoqi", label: "小七" },
            { value: "audit", label: "审计" },
          ]}
        />
        {/* 第 1 级 · 性质 */}
        <Select value={origin} onChange={(e) => setOrigin(e.target.value as typeof origin)} className="h-9 w-28">
          <option value="all">全部性质</option>
          <option value="live">正式</option>
          <option value="test">测试</option>
        </Select>
        <Select value={status} onChange={(e) => setStatus(e.target.value as typeof status)} className="h-9 w-28">
          <option value="all">全部状态</option>
          <option value="ok">成功</option>
          <option value="failed">失败</option>
        </Select>
        {/* 第 3 级 · 时机（阶段） */}
        <Select value={phase} onChange={(e) => setPhase(e.target.value)} className="h-9 w-36">
          <option value="all">全部环节</option>
          {phases.map((p) => (
            <option key={p} value={p}>{p}</option>
          ))}
        </Select>
        <div className="relative min-w-[160px] flex-1">
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
            <EmptyState icon={<ScrollText />} title="暂无日志" description="当前筛选条件下没有日志记录。" />
          ) : (
            <div className="divide-y divide-border">
              <div className="grid grid-cols-[72px_64px_1fr_120px_84px_64px_28px] items-center gap-2 px-4 py-2 text-xs font-medium text-muted-foreground">
                <span>类型</span>
                <span>性质</span>
                <span>接口 / 动作</span>
                <span>时机</span>
                <span>状态</span>
                <span>延迟</span>
                <span />
              </div>
              {rows.map((r) => {
                const Icon = KIND_META[r.kind].icon;
                const isOpen = full || expanded.has(r.id);
                const hasDetail = Boolean(details[r.id]);
                const isDetailLoading = loadingDetails.has(r.id);
                return (
                  <div key={r.id}>
                    <button
                      onClick={() => toggle(r)}
                      className="grid w-full grid-cols-[72px_64px_1fr_120px_84px_64px_28px] items-center gap-2 px-4 py-2.5 text-left text-sm transition-colors hover:bg-accent"
                    >
                      <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                        <Icon className="size-3.5" /> {KIND_META[r.kind].label}
                      </span>
                      <Badge variant={r.origin === "test" ? "warning" : "secondary"}>{originLabel(r.origin)}</Badge>
                      <span className="min-w-0">
                        <span className="block truncate font-mono text-xs text-foreground">{r.label}</span>
                        {r.detail && <span className="block truncate text-xs text-muted-foreground">{r.detail}</span>}
                        {(r.outputPreview || preview(r.output)) && (
                          <span className="block truncate text-xs text-primary/80">↳ 输出：{r.outputPreview || preview(r.output)}</span>
                        )}
                        <span className="block text-[11px] text-muted-foreground">{new Date(r.time).toLocaleString()}</span>
                      </span>
                      <span className="min-w-0 text-xs text-muted-foreground">
                        <span className="block truncate">{r.phase || "—"}</span>
                        {r.scene && <span className="block truncate opacity-70">{SCENE_LABELS[r.scene] ?? r.scene}</span>}
                      </span>
                      <Badge variant={r.ok ? "success" : "destructive"}>{r.status}</Badge>
                      <span className="text-xs text-muted-foreground">{r.latency != null ? `${r.latency}ms` : "—"}</span>
                      <ChevronDown className={`size-4 text-muted-foreground transition-transform ${isOpen ? "rotate-180" : ""}`} />
                    </button>
                    {isOpen && (
                      <div className="space-y-3 bg-muted/40 px-4 py-3">
                        {isDetailLoading && <p className="text-xs text-muted-foreground">正在加载完整输入 / 输出…</p>}
                        {!hasDetail && !isDetailLoading && r.requestPreview && <LogIO title="输入摘要" value={r.requestPreview} />}
                        {!hasDetail && !isDetailLoading && r.outputPreview && <LogIO title="输出摘要" value={r.outputPreview} />}
                        <LogIO title="输入" value={r.input} />
                        <LogIO title="输出" value={r.output} />
                        <details>
                          <summary className="cursor-pointer text-xs text-muted-foreground">{hasDetail ? "完整原始记录" : "摘要原始记录"}</summary>
                          <pre className="mt-2 overflow-x-auto text-xs text-foreground">{JSON.stringify(r.raw, null, 2)}</pre>
                        </details>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>
      {!full && rows.length > 0 && (
        <p className="text-center text-xs text-muted-foreground">共 {rows.length} 条 · 点击任意行展开完整输入 / 输出</p>
      )}
    </div>
  );
}

function LogIO({ title, value }: { title: string; value: unknown }) {
  if (value == null || value === "") return null;
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return (
    <div>
      <p className="mb-1 text-xs font-medium text-muted-foreground">{title}</p>
      <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded-md border border-border bg-background/40 p-2 text-xs text-foreground">{text}</pre>
    </div>
  );
}
