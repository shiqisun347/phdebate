import * as React from "react";
import { Mic, Volume2, AudioLines, CheckCircle2, XCircle, Play, Square, RefreshCw, Save } from "lucide-react";
import { Button, Card, CardContent, CardHeader, CardTitle, CardDescription, Input, Label, Switch, Badge, Textarea, Spinner, Separator } from "../ui/primitives";
import { useToast } from "../lib/toast";
import { useAdminData } from "../lib/data";
import { startPcmStream, createStreamingPlayer, base64ToBytes, type PcmStream } from "../lib/audio";
import { getIntegrationConfig, patchIntegrationConfig, getSpeechDiagnostics, testWsUrl } from "../../api/client";
import type { IntegrationConfig, IntegrationSection, SpeechDiagnostics } from "../../types/contracts";

type SecretDraft = { app_id: string; api_key: string; api_secret: string };
type SectionDraft = { enabled: boolean; endpoint: string; lang: string; voice: string; secrets: SecretDraft };

function toDraft(s: IntegrationSection): SectionDraft {
  return {
    enabled: s.enabled,
    endpoint: s.endpoint,
    lang: s.lang ?? "",
    voice: s.voice ?? "",
    secrets: { app_id: "", api_key: "", api_secret: "" },
  };
}

export function Speech() {
  const { matchId } = useAdminData();
  const toast = useToast();
  const [config, setConfig] = React.useState<IntegrationConfig | null>(null);
  const [diag, setDiag] = React.useState<SpeechDiagnostics | null>(null);

  const load = React.useCallback(async () => {
    try {
      const [c, d] = await Promise.all([getIntegrationConfig(matchId), getSpeechDiagnostics(matchId).catch(() => null)]);
      setConfig(c);
      setDiag(d);
    } catch (err) {
      toast(err instanceof Error ? err.message : "加载失败", "error");
    }
  }, [matchId, toast]);

  React.useEffect(() => {
    void load();
  }, [load]);

  if (!config) {
    return (
      <div className="flex items-center gap-2 py-20 text-muted-foreground">
        <Spinner /> 正在加载语音配置…
      </div>
    );
  }

  return (
    <div className="space-y-5">
      {diag && (
        <Card>
          <CardContent className="flex flex-wrap items-center gap-3 p-4">
            <Badge variant={diag.overall_status === "ready" ? "success" : diag.overall_status === "failed" ? "destructive" : "warning"}>
              引擎状态：{diag.overall_status}
            </Badge>
            <Badge variant="secondary">provider：{diag.provider}</Badge>
            <span className="text-xs text-muted-foreground">ASR {diag.asr.status} · TTS {diag.tts.status}</span>
            <Button size="sm" variant="ghost" className="ml-auto" onClick={load}>
              <RefreshCw /> 刷新
            </Button>
          </CardContent>
        </Card>
      )}

      <div className="grid gap-5 lg:grid-cols-2">
        <SectionEditor
          kind="asr"
          icon={<Mic className="size-5" />}
          title="ASR · 语音识别"
          desc="流式听写：把人类辩手的语音转写为文字。"
          section={config.asr}
          onSaved={load}
        />
        <SectionEditor
          kind="tts"
          icon={<Volume2 className="size-5" />}
          title="TTS · 语音合成"
          desc="把 AI 辩手的文本合成为语音播报。"
          section={config.tts}
          onSaved={load}
        />
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <AsrTester matchId={matchId} />
        <TtsTester matchId={matchId} />
      </div>
    </div>
  );
}

function SectionEditor({
  kind,
  icon,
  title,
  desc,
  section,
  onSaved,
}: {
  kind: "asr" | "tts";
  icon: React.ReactNode;
  title: string;
  desc: string;
  section: IntegrationSection;
  onSaved: () => Promise<void>;
}) {
  const toast = useToast();
  const { matchId } = useAdminData();
  const [d, setD] = React.useState<SectionDraft>(() => toDraft(section));
  const [saving, setSaving] = React.useState(false);
  React.useEffect(() => setD(toDraft(section)), [section]);

  const set = <K extends keyof SectionDraft>(k: K, v: SectionDraft[K]) => setD((p) => ({ ...p, [k]: v }));
  const setSecret = (k: keyof SecretDraft, v: string) => setD((p) => ({ ...p, secrets: { ...p.secrets, [k]: v } }));

  async function save() {
    setSaving(true);
    try {
      const patch: Record<string, unknown> = { enabled: d.enabled, endpoint: d.endpoint };
      if (kind === "asr") patch.lang = d.lang;
      else patch.voice = d.voice;
      const secrets: Record<string, string> = {};
      (Object.keys(d.secrets) as Array<keyof SecretDraft>).forEach((k) => {
        if (d.secrets[k].trim()) secrets[k] = d.secrets[k].trim();
      });
      if (Object.keys(secrets).length) patch.secrets = secrets;
      await patchIntegrationConfig(matchId, { [kind]: patch });
      toast(`${title} 已保存`, "success");
      await onSaved();
    } catch (err) {
      toast(err instanceof Error ? err.message : "保存失败", "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="flex size-10 items-center justify-center rounded-lg bg-primary/10 text-primary">{icon}</div>
            <div>
              <CardTitle>{title}</CardTitle>
              <CardDescription>{desc}</CardDescription>
            </div>
          </div>
          <Switch checked={d.enabled} onCheckedChange={(v) => set("enabled", v)} />
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="space-y-1.5">
          <Label>WebSocket 接口地址</Label>
          <Input value={d.endpoint} onChange={(e) => set("endpoint", e.target.value)} placeholder="wss://…" />
        </div>
        {kind === "asr" ? (
          <div className="space-y-1.5">
            <Label>识别语种 / 方言</Label>
            <Input value={d.lang} onChange={(e) => set("lang", e.target.value)} placeholder="autodialect" />
          </div>
        ) : (
          <div className="space-y-1.5">
            <Label>发音人 voice</Label>
            <Input value={d.voice} onChange={(e) => set("voice", e.target.value)} placeholder="x6_lingfeiyi_pro" />
          </div>
        )}
        <Separator />
        <p className="text-xs font-medium text-muted-foreground">讯飞密钥（留空表示不修改）</p>
        {(["app_id", "api_key", "api_secret"] as const).map((k) => (
          <div key={k} className="space-y-1.5">
            <Label className="flex items-center gap-2 text-xs">
              {k.toUpperCase()}
              {section.secrets[k].configured ? (
                <Badge variant="success">已配置</Badge>
              ) : (
                <Badge variant="muted">未配置</Badge>
              )}
            </Label>
            <Input
              type="password"
              value={d.secrets[k]}
              onChange={(e) => setSecret(k, e.target.value)}
              placeholder={section.secrets[k].configured ? "••••••••（已保存）" : "输入以设置"}
            />
          </div>
        ))}
        <div className="flex justify-end pt-1">
          <Button onClick={save} loading={saving}>
            <Save /> 保存{kind === "asr" ? " ASR" : " TTS"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

/* ---- ASR 流式测试：麦克风 → WebSocket → 实时 partial/final ---- */
function AsrTester({ matchId }: { matchId: string }) {
  const toast = useToast();
  const [level, setLevel] = React.useState(0);
  const [recording, setRecording] = React.useState(false);
  const [partial, setPartial] = React.useState("");
  const [finals, setFinals] = React.useState<string[]>([]);
  const [done, setDone] = React.useState<string | null>(null);
  const streamRef = React.useRef<PcmStream | null>(null);
  const wsRef = React.useRef<WebSocket | null>(null);
  const rafRef = React.useRef<number | null>(null);

  function meter(analyser: AnalyserNode) {
    const data = new Uint8Array(analyser.frequencyBinCount);
    const tick = () => {
      analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (const v of data) sum += (v - 128) ** 2;
      setLevel(Math.min(100, Math.round(Math.sqrt(sum / data.length) * 4)));
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
  }

  function cleanup() {
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    void streamRef.current?.stop();
    streamRef.current = null;
    setLevel(0);
    setRecording(false);
  }

  async function start() {
    setPartial("");
    setFinals([]);
    setDone(null);
    const ws = new WebSocket(testWsUrl("asr", matchId));
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;
    ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      if (m.type === "ready") void beginMic(ws);
      else if (m.type === "partial") setPartial(m.text || "");
      else if (m.type === "final") {
        setFinals((prev) => [...prev, m.text]);
        setPartial("");
      } else if (m.type === "done") {
        setDone(`识别完成 · ${m.chunk_count ?? 0} 段 · 延迟 ${m.latency_ms ?? 0}ms`);
        cleanup();
      } else if (m.type === "error") {
        toast(m.message || "ASR 流式错误", "error");
        cleanup();
      }
    };
    ws.onerror = () => toast("ASR 连接失败", "error");
    ws.onclose = () => cleanup();
  }

  async function beginMic(ws: WebSocket) {
    try {
      const s = await startPcmStream((frame) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(frame);
      });
      streamRef.current = s;
      meter(s.analyser);
      setRecording(true);
    } catch {
      toast("无法访问麦克风，请检查浏览器权限", "error");
      ws.close();
    }
  }

  async function stop() {
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    await streamRef.current?.stop();
    streamRef.current = null;
    setLevel(0);
    setRecording(false);
    if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send("end");
  }

  React.useEffect(() => () => {
    cleanup();
    wsRef.current?.close();
  }, []);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <Mic className="size-4" /> ASR 流式识别测试
        </CardTitle>
        <CardDescription>开始后边说边出字（实时 partial/final），停止结束本次会话。需配置实时听写地址。</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="h-3 w-full overflow-hidden rounded-full bg-muted">
          <div className="h-full rounded-full bg-success transition-all" style={{ width: `${level}%` }} />
        </div>
        <div className="flex gap-2">
          {recording ? (
            <Button variant="destructive" size="sm" onClick={stop}>
              <Square /> 停止
            </Button>
          ) : (
            <Button variant="outline" size="sm" onClick={start}>
              <Mic /> 开始流式识别
            </Button>
          )}
        </div>
        {(finals.length > 0 || partial || done) && (
          <div className="min-h-[3rem] space-y-1 rounded-md border border-border bg-muted/40 p-3 text-sm">
            {finals.map((t, i) => (
              <span key={i} className="text-foreground">{t}</span>
            ))}
            {partial && <span className="text-muted-foreground">{partial}</span>}
            {done && <p className="pt-1 text-xs text-success">{done}</p>}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/* ---- TTS 测试：扬声器测试 + 服务端合成自检 ---- */
function TtsTester({ matchId }: { matchId: string }) {
  const toast = useToast();
  const [text, setText] = React.useState("人机辩论赛语音合成自检。");
  const [probing, setProbing] = React.useState(false);
  const [result, setResult] = React.useState<string | null>(null);
  const [playing, setPlaying] = React.useState(false);

  function testSpeaker() {
    // 播放一段提示音，确认扬声器工作正常
    try {
      const ac = new AudioContext();
      const osc = ac.createOscillator();
      const gain = ac.createGain();
      osc.type = "sine";
      osc.frequency.value = 660;
      gain.gain.setValueAtTime(0.0001, ac.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.25, ac.currentTime + 0.05);
      gain.gain.exponentialRampToValueAtTime(0.0001, ac.currentTime + 0.6);
      osc.connect(gain).connect(ac.destination);
      osc.start();
      osc.stop(ac.currentTime + 0.65);
      setPlaying(true);
      window.setTimeout(() => {
        setPlaying(false);
        void ac.close();
      }, 800);
    } catch {
      toast("扬声器测试失败", "error");
    }
  }

  function synth() {
    setProbing(true);
    setResult(null);
    const startedAt = performance.now();
    let firstChunkAt = 0;
    let chunks = 0;
    let mime = "audio/mpeg";
    let player: ReturnType<typeof createStreamingPlayer> | null = null;
    const ws = new WebSocket(testWsUrl("tts", matchId));
    ws.onopen = () => ws.send(JSON.stringify({ text }));
    ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      if (m.type === "chunk") {
        chunks += 1;
        if (!firstChunkAt) {
          firstChunkAt = performance.now();
          mime = "audio/mpeg";
          player = createStreamingPlayer(mime);
        }
        player?.push(base64ToBytes(m.audio_base64));
        setResult(`流式接收中… 已收 ${chunks} 段`);
      } else if (m.type === "done") {
        player?.end();
        const first = firstChunkAt ? Math.round(firstChunkAt - startedAt) : m.latency_ms;
        setResult(`合成完成 · ${m.chunk_count} 段 · 首段 ${first}ms · 总 ${m.latency_ms}ms · ${m.mime_type}`);
        toast("TTS 流式合成完成", "success");
        setProbing(false);
        ws.close();
      } else if (m.type === "error") {
        toast(m.message || "TTS 流式失败", "error");
        setProbing(false);
        ws.close();
      }
    };
    ws.onerror = () => {
      toast("TTS 连接失败", "error");
      setProbing(false);
    };
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <Volume2 className="size-4" /> TTS 流式合成与播放测试
        </CardTitle>
        <CardDescription>边合成边播放（流式），并显示首段/总延迟与分段数。</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <Textarea rows={2} value={text} onChange={(e) => setText(e.target.value)} className="font-sans" />
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={testSpeaker} disabled={playing}>
            {playing ? <CheckCircle2 className="text-success" /> : <Play />} 测扬声器
          </Button>
          <Button size="sm" onClick={synth} loading={probing} disabled={!text.trim()}>
            <Volume2 /> 流式合成并播放
          </Button>
        </div>
        {result && (
          <div className="space-y-1 rounded-md border border-border bg-muted/40 p-3 text-xs">
            <p className="text-muted-foreground">请求文本：{text}</p>
            <p className="flex items-center gap-1.5 text-success">
              <CheckCircle2 className="size-3.5" /> {result}
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
