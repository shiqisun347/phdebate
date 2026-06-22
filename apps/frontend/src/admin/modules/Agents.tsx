import * as React from "react";
import { Bot, Plus, Pencil, Trash2, Code2, Wrench, KeyRound, Send, CheckCircle2, XCircle, Plug, ChevronDown } from "lucide-react";
import { Button, Card, CardContent, Badge, Input, Label, Select, Textarea, Switch, EmptyState, Separator, Spinner } from "../ui/primitives";
import { Dialog, DialogHeader, DialogBody, DialogFooter } from "../ui/Dialog";
import { useAdminData } from "../lib/data";
import { useAction } from "../lib/actions";
import { post, patch, remove, testAgentConfig, testAgentConfigInline } from "../../api/client";
import { sideLabel } from "../lib/labels";
import type { AgentConfig, AgentConfigTestResult, MatchSnapshot, Phase, Speaker } from "../../types/contracts";

const SEAT_LABELS = ["", "一辩", "二辩", "三辩", "四辩"];
const seatLabel = (seat: number) => SEAT_LABELS[seat] ?? `${seat}号位`;

const REQUEST_TEMPLATE = {
  model_name: "qwen3.6-plus",
  debater_name: "乾元",
  debate_position: "一辩",
  debate_topic: "AI时代，我们应该培养编程思维/提问思维",
  current_stage: "正方一辩立论",
  next_stage: "反方一辩立论",
  holder: "正方",
  other_info: {},
  debate_history: [
    { stage: "正方一辩立论", content: [{ speaker: "正方一辩", content: "……" }] },
  ],
};

/** 用真实比赛参数动态拼装发送给辩手的请求体。 */
function buildDynamicPayload(
  config: AgentConfig | undefined,
  speaker: Speaker | undefined,
  currentPhase: Phase | undefined,
  phases: Phase[],
  topic: string
): Record<string, unknown> {
  const ordered = [...phases].sort((a, b) => a.display_order - b.display_order);
  const idx = currentPhase ? ordered.findIndex((p) => p.id === currentPhase.id) : -1;
  const nextPhase = idx >= 0 ? ordered[idx + 1] : undefined;
  const requestModel = config?.model_id || config?.model_name || speaker?.model_name || "";
  const displayModel = config?.model_name || speaker?.model_name || "";
  return {
    model_name: requestModel,
    request_model: requestModel,
    model_display_name: displayModel,
    debater_name: speaker?.name || "测试辩手",
    debate_position: speaker ? seatLabel(speaker.seat) : "一辩",
    debate_topic: topic || "AI时代，我们应该培养编程思维/提问思维",
    current_stage: currentPhase?.name || "正方一辩立论",
    next_stage: nextPhase?.name || "比赛结束",
    holder: speaker ? sideLabel(speaker.side) : "正方",
    other_info: {},
    debate_history: [],
  };
}

type Draft = {
  name: string;
  provider_type: "rest_api" | "openai_sdk";
  request_method: string;
  model_name: string;
  model_id: string;
  model_kind: "open_source" | "closed_source";
  endpoint: string;
  base_url: string;
  api_key_env: string;
  timeout_ms: number;
  enabled: boolean;
};

const EMPTY: Draft = {
  name: "",
  provider_type: "rest_api",
  request_method: "POST",
  model_name: "",
  model_id: "qwen3.6-plus",
  model_kind: "closed_source",
  endpoint: "http://localhost:8000/api/debate",
  base_url: "",
  api_key_env: "",
  timeout_ms: 30000,
  enabled: true,
};

export function Agents() {
  const { snapshot, matchId } = useAdminData();
  const { run, pending } = useAction();
  const [editing, setEditing] = React.useState<AgentConfig | null>(null);
  const [creating, setCreating] = React.useState(false);
  const [showSchema, setShowSchema] = React.useState(false);
  const [debugging, setDebugging] = React.useState(false);

  const configs = snapshot?.agent_configs ?? [];

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">配置 AI 辩手的接入方式。REST API 直连辩论接口；SDK 走 OpenAI 兼容模型。</p>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => setShowSchema(true)}>
            <Code2 /> 输入输出格式
          </Button>
          <Button variant="outline" onClick={() => setDebugging(true)} disabled={configs.length === 0}>
            <Wrench /> Agent 调试
          </Button>
          <Button onClick={() => setCreating(true)}>
            <Plus /> 新增 Agent
          </Button>
        </div>
      </div>

      {configs.length === 0 ? (
        <EmptyState icon={<Bot />} title="还没有 Agent" description="新增一个 Agent 配置以接入 AI 辩手。" action={<Button onClick={() => setCreating(true)}><Plus /> 新增 Agent</Button>} />
      ) : (
        <div className="grid gap-3 md:grid-cols-2">
          {configs.map((c) => (
            <Card key={c.id}>
              <CardContent className="space-y-3 p-5">
                <div className="flex items-start justify-between gap-3">
                  <div className="flex items-center gap-3">
                    <div className="flex size-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
                      <Bot className="size-5" />
                    </div>
                    <div>
                      <p className="font-semibold text-foreground">{c.name}</p>
                      <p className="text-xs text-muted-foreground">
                        展示：{c.model_name || "未命名"}{c.model_id ? ` · 请求：${c.model_id}` : ""}
                      </p>
                    </div>
                  </div>
                  <Badge variant={c.enabled ? "success" : "muted"}>{c.enabled ? "启用" : "停用"}</Badge>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  <Badge variant="secondary">{c.provider_type === "rest_api" ? "REST API" : "SDK"}</Badge>
                  <Badge variant="outline">{c.request_method}</Badge>
                  <Badge variant="outline">{c.model_kind === "open_source" ? "开源" : "闭源"}</Badge>
                  <Badge variant="muted">{c.timeout_ms}ms</Badge>
                </div>
                <p className="truncate font-mono text-xs text-muted-foreground" title={c.provider_type === "rest_api" ? c.endpoint : c.base_url}>
                  {c.provider_type === "rest_api" ? c.endpoint || "未设置接口地址" : c.base_url || "未设置 base_url"}
                </p>
                {c.api_key_env && (
                  <p className="flex items-center gap-1 text-xs text-muted-foreground">
                    <KeyRound className="size-3" /> {c.api_key_env}
                  </p>
                )}
                <Separator />
                <div className="flex justify-end gap-2">
                  <Button size="sm" variant="ghost" onClick={() => setEditing(c)}>
                    <Pencil /> 编辑
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="text-destructive hover:bg-destructive/10"
                    onClick={() => {
                      if (confirm(`确认删除 Agent「${c.name}」？`))
                        run(() => remove(`/api/matches/${matchId}/agents/configs/${c.id}`), { success: "已删除" });
                    }}
                  >
                    <Trash2 /> 删除
                  </Button>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {(creating || editing) && (
        <AgentDialog
          initial={editing ? toDraft(editing) : EMPTY}
          title={editing ? "编辑 Agent" : "新增 Agent"}
          matchId={matchId}
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          onSubmit={async (draft) => {
            const ok = await run(
              () =>
                editing
                  ? patch(`/api/matches/${matchId}/agents/configs/${editing.id}`, draft)
                  : post(`/api/matches/${matchId}/agents/configs`, draft),
              { success: editing ? "已保存" : "已创建" }
            );
            if (ok) {
              setCreating(false);
              setEditing(null);
            }
          }}
          pending={pending}
        />
      )}

      <Dialog open={showSchema} onClose={() => setShowSchema(false)} size="lg">
        <DialogHeader title="辩手请求 / 响应格式" description="发送给 AI 辩手的结构化请求体（参考 需求 admin.md §5）" onClose={() => setShowSchema(false)} />
        <DialogBody>
          <Textarea readOnly rows={16} value={JSON.stringify(REQUEST_TEMPLATE, null, 2)} className="text-xs" />
          <p className="text-xs text-muted-foreground">REST API agent 将以上结构通过所配置的请求方式发送至接口地址；响应应返回辩手发言文本（支持流式）。</p>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={() => setShowSchema(false)}>关闭</Button>
        </DialogFooter>
      </Dialog>

      {debugging && <AgentDebugDialog configs={configs} snapshot={snapshot} matchId={matchId} onClose={() => setDebugging(false)} />}
    </div>
  );
}

/* ------------------------- Agent 调试对话框 ------------------------- */
function TestResultView({ result }: { result: AgentConfigTestResult }) {
  const [showJson, setShowJson] = React.useState(false);
  return (
    <div className={`space-y-1.5 rounded-md border p-3 text-xs ${result.ok ? "border-success/40 bg-success/5" : "border-destructive/40 bg-destructive/5"}`}>
      <p className={`flex items-center gap-1.5 font-medium ${result.ok ? "text-success" : "text-destructive"}`}>
        {result.ok ? <CheckCircle2 className="size-3.5" /> : <XCircle className="size-3.5" />}
        {result.ok ? `连通成功 · ${result.latency_ms}ms · ${result.model ?? ""}` : `失败：${result.error_code ?? ""} ${result.error_message ?? ""}`}
      </p>
      {result.ok && (
        <div className="max-h-48 overflow-y-auto whitespace-pre-wrap rounded bg-card p-2 text-foreground">
          {result.content || "(无返回文本)"}
        </div>
      )}
      <button
        type="button"
        onClick={() => setShowJson((v) => !v)}
        className="flex items-center gap-1 text-muted-foreground hover:text-foreground"
      >
        <ChevronDown className={`size-3.5 transition-transform ${showJson ? "rotate-180" : ""}`} /> 完整 JSON（请求与输出）
      </button>
      {showJson && (
        <pre className="max-h-72 overflow-auto rounded bg-muted/50 p-2 text-foreground">{JSON.stringify(result, null, 2)}</pre>
      )}
    </div>
  );
}

function AgentDebugDialog({
  configs,
  snapshot,
  matchId,
  onClose,
}: {
  configs: AgentConfig[];
  snapshot: MatchSnapshot | null;
  matchId: string;
  onClose: () => void;
}) {
  const speakers = React.useMemo(() => (snapshot?.speakers ?? []).filter((s) => s.speaker_type === "agent"), [snapshot]);
  const phases = snapshot?.phases ?? [];
  const topic = snapshot?.match.topic ?? "";

  const [configId, setConfigId] = React.useState(configs[0]?.id ?? "");
  const [speakerId, setSpeakerId] = React.useState(speakers[0]?.id ?? "");
  const [phaseId, setPhaseId] = React.useState(snapshot?.match.current_phase_id ?? phases[0]?.id ?? "");
  const [payloadText, setPayloadText] = React.useState("");
  const [edited, setEdited] = React.useState(false);
  const [testing, setTesting] = React.useState(false);
  const [result, setResult] = React.useState<AgentConfigTestResult | null>(null);
  const [err, setErr] = React.useState<string | null>(null);

  const config = configs.find((c) => c.id === configId);
  const speaker = speakers.find((s) => s.id === speakerId);
  const currentPhase = phases.find((p) => p.id === phaseId);

  // 选择项变化时，自动用真实参数重建请求体（除非用户手动改过）。
  React.useEffect(() => {
    if (edited) return;
    setPayloadText(JSON.stringify(buildDynamicPayload(config, speaker, currentPhase, phases, topic), null, 2));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [configId, speakerId, phaseId, edited]);

  function resetPayload() {
    setEdited(false);
    setPayloadText(JSON.stringify(buildDynamicPayload(config, speaker, currentPhase, phases, topic), null, 2));
  }

  async function send() {
    let payload: Record<string, unknown> | undefined;
    if (payloadText.trim()) {
      try {
        payload = JSON.parse(payloadText);
      } catch {
        setErr("请求体不是合法 JSON");
        return;
      }
    }
    setErr(null);
    setTesting(true);
    setResult(null);
    try {
      setResult(await testAgentConfig(matchId, configId, payload));
    } catch (e) {
      setErr(e instanceof Error ? e.message : "测试失败");
    } finally {
      setTesting(false);
    }
  }

  return (
    <Dialog open onClose={onClose} size="lg">
      <DialogHeader title="Agent 调试" description="选择 Agent 与真实比赛参数，自动拼装请求体后发起测试。请求体可手动编辑。" onClose={onClose} />
      <DialogBody>
        <div className="grid gap-3 sm:grid-cols-3">
          <div className="space-y-1.5">
            <Label>Agent</Label>
            <Select value={configId} onChange={(e) => setConfigId(e.target.value)}>
              {configs.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}（{c.provider_type === "rest_api" ? "REST" : "SDK"}）
                </option>
              ))}
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>辩手（来自当前比赛）</Label>
            <Select value={speakerId} onChange={(e) => setSpeakerId(e.target.value)} disabled={speakers.length === 0}>
              {speakers.length === 0 ? (
                <option value="">无 AI 辩手</option>
              ) : (
                speakers.map((s) => (
                  <option key={s.id} value={s.id}>
                    {sideLabel(s.side)}{seatLabel(s.seat)} · {s.name}
                  </option>
                ))
              )}
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>当前环节</Label>
            <Select value={phaseId} onChange={(e) => setPhaseId(e.target.value)}>
              {[...phases]
                .sort((a, b) => a.display_order - b.display_order)
                .map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
            </Select>
          </div>
        </div>
        <div className="space-y-1.5">
          <div className="flex items-center justify-between">
            <Label>测试请求体（JSON · 动态填充）</Label>
            <Button variant="ghost" size="sm" onClick={resetPayload}>按所选重置</Button>
          </div>
          <Textarea
            rows={12}
            value={payloadText}
            onChange={(e) => {
              setPayloadText(e.target.value);
              setEdited(true);
            }}
            className="text-xs"
          />
          {err && <p className="text-xs text-destructive">{err}</p>}
        </div>
        {result && <TestResultView result={result} />}
      </DialogBody>
      <DialogFooter>
        <Button variant="outline" onClick={onClose}>关闭</Button>
        <Button onClick={send} loading={testing} disabled={!configId}>
          <Send /> 发送测试
        </Button>
      </DialogFooter>
    </Dialog>
  );
}

function toDraft(c: AgentConfig): Draft {
  return {
    name: c.name,
    provider_type: (c.provider_type as Draft["provider_type"]) ?? "rest_api",
    request_method: c.request_method ?? "POST",
    model_name: c.model_name ?? "",
    model_id: c.model_id || "qwen3.6-plus",
    model_kind: (c.model_kind as Draft["model_kind"]) ?? "closed_source",
    endpoint: c.endpoint ?? "",
    base_url: c.base_url ?? "",
    api_key_env: c.api_key_env ?? "",
    timeout_ms: c.timeout_ms ?? 30000,
    enabled: c.enabled,
  };
}

function AgentDialog({
  initial,
  title,
  matchId,
  onClose,
  onSubmit,
  pending,
}: {
  initial: Draft;
  title: string;
  matchId: string;
  onClose: () => void;
  onSubmit: (d: Draft) => void;
  pending: boolean;
}) {
  const [d, setD] = React.useState<Draft>(initial);
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setD((p) => ({ ...p, [k]: v }));
  const isRest = d.provider_type === "rest_api";
  const [testing, setTesting] = React.useState(false);
  const [testResult, setTestResult] = React.useState<AgentConfigTestResult | null>(null);

  async function testConnectivity() {
    setTesting(true);
    setTestResult(null);
    try {
      setTestResult(await testAgentConfigInline(matchId, { ...d }));
    } catch (e) {
      setTestResult({ ok: false, error_message: e instanceof Error ? e.message : "测试失败" });
    } finally {
      setTesting(false);
    }
  }

  return (
    <Dialog open onClose={onClose} size="lg">
      <DialogHeader title={title} description="REST API 直连辩论接口；SDK 走 OpenAI 兼容模型。" onClose={onClose} />
      <DialogBody>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Agent 名称" required>
            <Input value={d.name} onChange={(e) => set("name", e.target.value)} placeholder="如：乾元" />
          </Field>
          <Field label="Agent 类型">
            <Select value={d.provider_type} onChange={(e) => set("provider_type", e.target.value as Draft["provider_type"])}>
              <option value="rest_api">REST API agent</option>
              <option value="openai_sdk">SDK agent（OpenAI 兼容）</option>
            </Select>
          </Field>
          <Field label="请求方式">
            <Select value={d.request_method} onChange={(e) => set("request_method", e.target.value)}>
              {["POST", "GET", "PUT", "PATCH"].map((m) => (
                <option key={m}>{m}</option>
              ))}
            </Select>
          </Field>
          <Field label="模型类型">
            <Select value={d.model_kind} onChange={(e) => set("model_kind", e.target.value as Draft["model_kind"])}>
              <option value="closed_source">闭源</option>
              <option value="open_source">开源</option>
            </Select>
          </Field>
          <Field label="展示名称">
            <Input value={d.model_name} onChange={(e) => set("model_name", e.target.value)} placeholder="如：墨辩 Agent / Qwen-Max" />
          </Field>
          <Field label="请求模型 ID" hint="真实请求中的 model_name 会使用该值，例如 qwen3.6-plus。">
            <Input value={d.model_id} onChange={(e) => set("model_id", e.target.value)} placeholder="qwen3.6-plus" />
          </Field>
          {isRest ? (
            <Field label="接口地址 endpoint" required>
              <Input value={d.endpoint} onChange={(e) => set("endpoint", e.target.value)} placeholder="http://localhost:8000/api/debate" />
            </Field>
          ) : (
            <>
              <Field label="Base URL" required>
                <Input value={d.base_url} onChange={(e) => set("base_url", e.target.value)} placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1" />
              </Field>
            </>
          )}
          <Field label="API Key 环境变量名" hint="不保存明文，填写服务器环境变量名">
            <Input value={d.api_key_env} onChange={(e) => set("api_key_env", e.target.value)} placeholder="如：DASHSCOPE_API_KEY" />
          </Field>
          <Field label="超时（毫秒）">
            <Input
              type="number"
              min={1000}
              max={120000}
              value={d.timeout_ms}
              onChange={(e) => set("timeout_ms", Number(e.target.value))}
            />
          </Field>
        </div>
        <label className="flex items-center gap-2.5 pt-1">
          <Switch checked={d.enabled} onCheckedChange={(v) => set("enabled", v)} />
          <span className="text-sm text-foreground">启用该 Agent</span>
        </label>
        {testResult && <TestResultView result={testResult} />}
      </DialogBody>
      <DialogFooter>
        <Button variant="outline" onClick={testConnectivity} loading={testing} disabled={!d.name.trim()} className="mr-auto">
          <Plug /> 测试连通
        </Button>
        <Button variant="outline" onClick={onClose}>取消</Button>
        <Button loading={pending} onClick={() => onSubmit(d)} disabled={!d.name.trim()}>
          保存
        </Button>
      </DialogFooter>
    </Dialog>
  );
}

function Field({ label, required, hint, children }: { label: string; required?: boolean; hint?: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <Label>
        {label}
        {required && <span className="ml-0.5 text-destructive">*</span>}
      </Label>
      {children}
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}
