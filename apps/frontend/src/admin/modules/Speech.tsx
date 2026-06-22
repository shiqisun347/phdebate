import * as React from "react";
import { CheckCircle2, Mic, Play, Plus, RefreshCw, Save, Square, Trash2, Volume2 } from "lucide-react";
import { Badge, Button, Card, CardContent, CardDescription, CardHeader, CardTitle, Input, Label, Select, Separator, Spinner, Switch, Textarea } from "../ui/primitives";
import { useToast } from "../lib/toast";
import { useAdminData } from "../lib/data";
import { base64ToBytes, startPcmStream, type PcmStream } from "../lib/audio";
import { getIntegrationConfig, getSpeechDiagnostics, patchIntegrationConfig, testWsUrl } from "../../api/client";
import type { IntegrationConfig, IntegrationSection, SpeechDiagnostics, SpeechProvider, VoicePreset } from "../../types/contracts";

const ALICLOUD_ASR_DEFAULTS = {
  endpoint: "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3-asr-flash-realtime",
  settings: {
    model: "qwen3-asr-flash-realtime",
    input_audio_format: "pcm",
    sample_rate: 16000,
    language: "zh",
    turn_detection: { type: "server_vad", threshold: 0, silence_duration_ms: 400 },
  },
};

const ALICLOUD_TTS_DEFAULTS = {
  endpoint: "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3-tts-flash-realtime",
  settings: {
    model: "qwen3-tts-flash-realtime",
    response_format: "mp3",
    sample_rate: 24000,
    mode: "server_commit",
    language_type: "Chinese",
  },
};

const XFYUN_ASR_DEFAULTS = {
  endpoint: "wss://office-api-ast-dx.iflyaisol.com/ast/communicate/v1",
  settings: { model: "xfyun-rtasr", input_audio_format: "pcm", sample_rate: 16000, language: "autodialect" },
};

const XFYUN_TTS_DEFAULTS = {
  endpoint: "wss://cbm01.cn-huabei-1.xf-yun.com/v1/private/mcd9m97e6",
  settings: { model: "xfyun-super-tts", response_format: "mp3", sample_rate: 24000, mode: "server_commit", language_type: "Chinese" },
};

const ALICLOUD_VOICES = [
  { voice: "Neil", label: "Neil · 阿闻 · 清晰男声辩手" },
  { voice: "Ethan", label: "Ethan · 晨煦 · 备用男声" },
  { voice: "Serena", label: "Serena · 苏瑶 · 女声辩手" },
  { voice: "Cherry", label: "Cherry · 芊悦 · 主持播报" },
];

const XFYUN_VOICES = [
  { voice: "x6_lingfeiyi_pro", label: "x6_lingfeiyi_pro" },
  { voice: "x6_lingxiaoxuan_pro", label: "x6_lingxiaoxuan_pro" },
];

type SecretDraft = {
  xfyun_app_id: string;
  xfyun_api_key: string;
  xfyun_api_secret: string;
  alicloud_api_key: string;
  alicloud_workspace_id: string;
};
type SectionDraft = {
  enabled: boolean;
  provider: SpeechProvider;
  endpoint: string;
  lang: string;
  voice: string;
  settings: Record<string, unknown>;
  secrets: SecretDraft;
};

function toDraft(section: IntegrationSection, kind: "asr" | "tts"): SectionDraft {
  const provider = (section.provider === "xfyun" ? "xfyun" : "alicloud") as SpeechProvider;
  const defaults = providerDefaults(kind, provider);
  return {
    enabled: section.enabled,
    provider,
    endpoint: section.endpoint || defaults.endpoint,
    lang: section.lang ?? "",
    voice: section.voice ?? "",
    settings: { ...defaults.settings, ...(section.settings ?? {}) },
    secrets: {
      xfyun_app_id: "",
      xfyun_api_key: "",
      xfyun_api_secret: "",
      alicloud_api_key: "",
      alicloud_workspace_id: "",
    },
  };
}

function providerDefaults(kind: "asr" | "tts", provider: SpeechProvider) {
  if (provider === "xfyun") return kind === "asr" ? XFYUN_ASR_DEFAULTS : XFYUN_TTS_DEFAULTS;
  return kind === "asr" ? ALICLOUD_ASR_DEFAULTS : ALICLOUD_TTS_DEFAULTS;
}

function withProviderDefaults(kind: "asr" | "tts", provider: SpeechProvider, current?: SectionDraft): SectionDraft {
  const defaults = providerDefaults(kind, provider);
  const defaultSettings = defaults.settings as Record<string, unknown>;
  return {
    ...(current ?? {
      enabled: true,
      provider,
      endpoint: defaults.endpoint,
      lang: "",
      voice: "",
      settings: {},
      secrets: {
        xfyun_app_id: "",
        xfyun_api_key: "",
        xfyun_api_secret: "",
        alicloud_api_key: "",
        alicloud_workspace_id: "",
      },
    }),
    provider,
    endpoint: defaults.endpoint,
    lang: kind === "asr" ? String(defaultSettings.language ?? "") : current?.lang ?? "",
    voice: provider === "xfyun" && kind === "tts" ? "x6_lingfeiyi_pro" : current?.voice ?? "",
    settings: { ...defaults.settings },
  };
}


function configured(section: IntegrationSection, provider: "xfyun" | "alicloud", key: "app_id" | "api_key" | "api_secret" | "workspace_id") {
  if (provider === "xfyun") {
    const rootKey = key as "app_id" | "api_key" | "api_secret";
    return Boolean(section.secrets?.xfyun?.[key]?.configured ?? section.secrets?.[rootKey]?.configured);
  }
  return Boolean(section.secrets?.alicloud?.[key]?.configured);
}

function fmtProvider(value: unknown) {
  if (typeof value === "object" && value) {
    const item = value as { asr?: string; tts?: string };
    return `ASR ${item.asr ?? "-"} · TTS ${item.tts ?? "-"}`;
  }
  return String(value || "-");
}

export function Speech() {
  const { matchId, snapshot, refresh } = useAdminData();
  const toast = useToast();
  const [config, setConfig] = React.useState<IntegrationConfig | null>(snapshot?.integration_config ?? null);
  const [diag, setDiag] = React.useState<SpeechDiagnostics | null>(null);

  const load = React.useCallback(async () => {
    try {
      const [nextConfig, nextDiag] = await Promise.all([getIntegrationConfig(matchId), getSpeechDiagnostics(matchId).catch(() => null)]);
      setConfig(nextConfig);
      setDiag(nextDiag);
      await refresh();
    } catch (err) {
      toast(err instanceof Error ? err.message : "加载失败", "error");
    }
  }, [matchId, refresh, toast]);

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

  const assignedIds = new Set((snapshot?.speakers ?? []).map((speaker) => speaker.tts_voice_preset_id).filter(Boolean) as string[]);

  return (
    <div className="space-y-5">
      {diag && (
        <Card>
          <CardContent className="flex flex-wrap items-center gap-3 p-4">
            <Badge variant={diag.overall_status === "ready" ? "success" : diag.overall_status === "failed" ? "destructive" : "warning"}>
              引擎状态：{diag.overall_status}
            </Badge>
            <Badge variant="secondary">{fmtProvider(diag.provider)}</Badge>
            <span className="text-xs text-muted-foreground">ASR {diag.asr.status} · TTS {diag.tts.status}</span>
            <Button size="sm" variant="ghost" className="ml-auto" onClick={load}>
              <RefreshCw /> 刷新
            </Button>
          </CardContent>
        </Card>
      )}

      <div className="grid gap-5 lg:grid-cols-2">
        <SectionEditor kind="asr" icon={<Mic className="size-5" />} title="ASR · 语音识别" desc="流式听写：把人类辩手的语音转写为文字。" section={config.asr} onSaved={load} />
        <SectionEditor kind="tts" icon={<Volume2 className="size-5" />} title="TTS · 语音合成" desc="把 AI 辩手的文本合成为语音播报。" section={config.tts} onSaved={load} />
      </div>

      <VoicePresetManager config={config} assignedIds={assignedIds} onSaved={load} />

      <div className="grid gap-5 lg:grid-cols-2">
        <AsrTester matchId={matchId} />
        <TtsTester matchId={matchId} presets={config.voice_presets.filter((item) => item.enabled && item.provider === config.tts.provider)} />
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
  const [draft, setDraft] = React.useState<SectionDraft>(() => toDraft(section, kind));
  const [saving, setSaving] = React.useState(false);

  React.useEffect(() => setDraft(toDraft(section, kind)), [section, kind]);

  const set = <K extends keyof SectionDraft>(key: K, value: SectionDraft[K]) => setDraft((prev) => ({ ...prev, [key]: value }));
  const setSetting = (key: string, value: unknown) => setDraft((prev) => ({ ...prev, settings: { ...prev.settings, [key]: value } }));
  const setVad = (key: string, value: number) =>
    setDraft((prev) => ({
      ...prev,
      settings: {
        ...prev.settings,
        turn_detection: { ...((prev.settings.turn_detection as Record<string, unknown>) ?? {}), type: "server_vad", [key]: value },
      },
    }));
  const setSecret = (key: keyof SecretDraft, value: string) => setDraft((prev) => ({ ...prev, secrets: { ...prev.secrets, [key]: value } }));

  async function save() {
    setSaving(true);
    try {
      const settings = { ...draft.settings };
      const patch: Record<string, unknown> = {
        enabled: draft.enabled,
        provider: draft.provider,
        endpoint: draft.endpoint.trim(),
        settings,
      };
      if (kind === "asr") patch.lang = draft.lang.trim();
      if (kind === "tts") patch.voice = draft.voice.trim();
      const secrets: Record<string, unknown> = {};
      if (draft.provider === "xfyun") {
        const xfyun: Record<string, string> = {};
        if (draft.secrets.xfyun_app_id.trim()) xfyun.app_id = draft.secrets.xfyun_app_id.trim();
        if (draft.secrets.xfyun_api_key.trim()) xfyun.api_key = draft.secrets.xfyun_api_key.trim();
        if (draft.secrets.xfyun_api_secret.trim()) xfyun.api_secret = draft.secrets.xfyun_api_secret.trim();
        if (Object.keys(xfyun).length) Object.assign(secrets, xfyun, { xfyun });
      } else {
        const alicloud: Record<string, string> = {};
        if (draft.secrets.alicloud_api_key.trim()) alicloud.api_key = draft.secrets.alicloud_api_key.trim();
        if (draft.secrets.alicloud_workspace_id.trim()) alicloud.workspace_id = draft.secrets.alicloud_workspace_id.trim();
        if (Object.keys(alicloud).length) secrets.alicloud = alicloud;
      }
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

  const alicloud = draft.provider === "alicloud";
  const modelDefault = kind === "asr" ? "qwen3-asr-flash-realtime" : "qwen3-tts-flash-realtime";
  const vad = (draft.settings.turn_detection as Record<string, unknown>) ?? {};

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="flex size-10 items-center justify-center rounded-lg bg-primary/10 text-primary">{icon}</div>
            <div>
              <CardTitle>{title}</CardTitle>
              <CardDescription>{desc}</CardDescription>
            </div>
          </div>
          <Switch checked={draft.enabled} onCheckedChange={(value) => set("enabled", value)} />
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>服务商</Label>
            <Select
              value={draft.provider}
              onChange={(event) => setDraft((prev) => withProviderDefaults(kind, event.target.value as SpeechProvider, prev))}
            >
              <option value="alicloud">阿里云百炼 Qwen</option>
              <option value="xfyun">讯飞</option>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>模型</Label>
            <Input value={String(draft.settings.model ?? modelDefault)} onChange={(event) => setSetting("model", event.target.value)} />
          </div>
        </div>

        <div className="space-y-1.5">
          <Label>WebSocket 接口地址</Label>
          <Input value={draft.endpoint} onChange={(event) => set("endpoint", event.target.value)} placeholder="wss://…" />
        </div>

        {kind === "asr" ? (
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="space-y-1.5">
              <Label>语言</Label>
              <Input value={String(draft.settings.language ?? draft.lang ?? "zh")} onChange={(event) => setSetting("language", event.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label>采样率</Label>
              <Input type="number" value={String(draft.settings.sample_rate ?? 16000)} onChange={(event) => setSetting("sample_rate", Number(event.target.value) || 16000)} />
            </div>
            <div className="space-y-1.5">
              <Label>音频格式</Label>
              <Input value={String(draft.settings.input_audio_format ?? "pcm")} onChange={(event) => setSetting("input_audio_format", event.target.value)} />
            </div>
          </div>
        ) : null}

        {kind === "asr" && alicloud && (
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label>VAD 阈值</Label>
              <Input type="number" step="0.1" value={String(vad.threshold ?? 0)} onChange={(event) => setVad("threshold", Number(event.target.value) || 0)} />
            </div>
            <div className="space-y-1.5">
              <Label>静默结束 ms</Label>
              <Input type="number" value={String(vad.silence_duration_ms ?? 400)} onChange={(event) => setVad("silence_duration_ms", Number(event.target.value) || 400)} />
            </div>
          </div>
        )}

        <Separator />
        {alicloud ? (
          <div className="grid gap-3 sm:grid-cols-2">
            <SecretInput label="DashScope API Key" configured={configured(section, "alicloud", "api_key")} value={draft.secrets.alicloud_api_key} onChange={(value) => setSecret("alicloud_api_key", value)} />
            <SecretInput label="Workspace ID" configured={configured(section, "alicloud", "workspace_id")} value={draft.secrets.alicloud_workspace_id} onChange={(value) => setSecret("alicloud_workspace_id", value)} />
          </div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-3">
            <SecretInput label="APP ID" configured={configured(section, "xfyun", "app_id")} value={draft.secrets.xfyun_app_id} onChange={(value) => setSecret("xfyun_app_id", value)} />
            <SecretInput label="API Key" configured={configured(section, "xfyun", "api_key")} value={draft.secrets.xfyun_api_key} onChange={(value) => setSecret("xfyun_api_key", value)} />
            <SecretInput label="API Secret" configured={configured(section, "xfyun", "api_secret")} value={draft.secrets.xfyun_api_secret} onChange={(value) => setSecret("xfyun_api_secret", value)} />
          </div>
        )}

        <div className="flex justify-end pt-1">
          <Button onClick={save} loading={saving}>
            <Save /> 保存{kind === "asr" ? " ASR" : " TTS"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function SecretInput({ label, configured: isConfigured, value, onChange }: { label: string; configured: boolean; value: string; onChange: (value: string) => void }) {
  return (
    <div className="space-y-1.5">
      <Label className="flex items-center gap-2 text-xs">
        {label}
        <Badge variant={isConfigured ? "success" : "muted"}>{isConfigured ? "已配置" : "未配置"}</Badge>
      </Label>
      <Input type="password" value={value} onChange={(event) => onChange(event.target.value)} placeholder={isConfigured ? "••••••••（留空不修改）" : "输入以设置"} />
    </div>
  );
}

function VoicePresetManager({
  config,
  assignedIds,
  onSaved,
}: {
  config: IntegrationConfig;
  assignedIds: Set<string>;
  onSaved: () => Promise<void>;
}) {
  const { matchId } = useAdminData();
  const toast = useToast();
  const blank = React.useMemo<VoicePreset>(
    () => ({
      id: "",
      name: "",
      provider: config.tts.provider,
      model: String(config.tts.settings?.model ?? "qwen3-tts-flash-realtime"),
      voice: "",
      response_format: String(config.tts.settings?.response_format ?? "mp3"),
      mode: String(config.tts.settings?.mode ?? "server_commit"),
      language_type: String(config.tts.settings?.language_type ?? "Chinese"),
      enabled: true,
      is_default: false,
      description: "",
      sample_rate: Number(config.tts.settings?.sample_rate ?? 24000),
      speech_rate: 1,
      volume: 70,
      pitch_rate: 1,
      instructions: "",
    }),
    [config.tts.provider, config.tts.settings]
  );
  const [editing, setEditing] = React.useState<VoicePreset | null>(null);
  // 自定义音色：除内置预设外，允许直接输入服务商的音色 ID（如复刻音色 / 其它官方发音人）。
  const [customVoice, setCustomVoice] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const visiblePresets = config.voice_presets.filter((preset) => preset.provider === config.tts.provider);
  const baseVoices = config.tts.provider === "xfyun" ? XFYUN_VOICES : ALICLOUD_VOICES;
  const isBaseVoice = (voice: string) => baseVoices.some((item) => item.voice === voice);

  function openEditor(preset: VoicePreset | null) {
    const next = preset ?? blank;
    setEditing(next);
    setCustomVoice(Boolean(next.voice) && !isBaseVoice(next.voice));
  }

  async function savePreset() {
    if (!editing?.name.trim() || !editing.voice.trim()) {
      toast("请填写音色名称并选择音色", "error");
      return;
    }
    setSaving(true);
    try {
      const preset = {
        ...editing,
        id: editing.id || `voice_${config.tts.provider}_${Date.now()}`,
        provider: config.tts.provider,
        model: String(config.tts.settings?.model ?? (config.tts.provider === "xfyun" ? "xfyun-super-tts" : "qwen3-tts-flash-realtime")),
        response_format: String(config.tts.settings?.response_format ?? "mp3"),
        sample_rate: Number(config.tts.settings?.sample_rate ?? 24000),
        mode: String(config.tts.settings?.mode ?? "server_commit"),
        language_type: String(config.tts.settings?.language_type ?? "Chinese"),
      };
      let next = config.voice_presets.filter((item) => item.id !== preset.id);
      if (preset.is_default) {
        next = next.map((item) => (item.provider === preset.provider ? { ...item, is_default: false } : item));
      }
      next = [...next, preset];
      await patchIntegrationConfig(matchId, { voice_presets: next });
      toast("音色预设已保存", "success");
      setEditing(null);
      await onSaved();
    } catch (err) {
      toast(err instanceof Error ? err.message : "保存音色失败", "error");
    } finally {
      setSaving(false);
    }
  }

  async function deletePreset(id: string) {
    if (assignedIds.has(id)) {
      toast("该音色已被辩手引用，不能删除", "error");
      return;
    }
    await patchIntegrationConfig(matchId, { voice_presets: config.voice_presets.filter((item) => item.id !== id) });
    toast("音色预设已删除", "success");
    await onSaved();
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <div>
            <CardTitle>音色预设库</CardTitle>
            <CardDescription>辩手管理只能从这里已启用的预设中选择。</CardDescription>
          </div>
          <Button size="sm" onClick={() => openEditor(null)}>
            <Plus /> 新建音色
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 lg:grid-cols-3">
          {visiblePresets.map((preset) => (
            <div key={preset.id} className="space-y-3 rounded-md border border-border p-3">
              <div className="flex items-start justify-between gap-2">
                <div>
                  <p className="font-medium">{preset.name}</p>
                  <p className="text-xs text-muted-foreground">
                    {preset.voice} · 语速 {preset.speech_rate ?? 1} · 音量 {preset.volume ?? 70}
                  </p>
                </div>
                <div className="flex gap-1">
                  {preset.is_default && <Badge variant="success">默认</Badge>}
                  <Badge variant={preset.enabled ? "secondary" : "muted"}>{preset.enabled ? "启用" : "停用"}</Badge>
                </div>
              </div>
              {preset.description && <p className="text-xs text-muted-foreground">{preset.description}</p>}
              <div className="flex flex-wrap gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() =>
                    openEditor({
                      ...blank,
                      ...preset,
                      provider: config.tts.provider,
                      model: String(config.tts.settings?.model ?? preset.model),
                      response_format: String(config.tts.settings?.response_format ?? preset.response_format),
                      sample_rate: Number(config.tts.settings?.sample_rate ?? preset.sample_rate ?? 24000),
                      mode: String(config.tts.settings?.mode ?? preset.mode),
                      language_type: String(config.tts.settings?.language_type ?? preset.language_type),
                    })
                  }
                >
                  编辑
                </Button>
                <Button size="sm" variant="ghost" disabled={assignedIds.has(preset.id)} onClick={() => void deletePreset(preset.id)}>
                  <Trash2 /> 删除
                </Button>
              </div>
            </div>
          ))}
        </div>

        {editing && (
          <div className="space-y-3 rounded-md border border-border bg-muted/30 p-4">
            <div className="grid gap-3 md:grid-cols-3">
              <Field label="名称" value={editing.name} onChange={(value) => setEditing({ ...editing, name: value })} />
              <div className="space-y-1.5">
                <Label>音色</Label>
                <Select
                  value={customVoice ? "__custom__" : editing.voice}
                  onChange={(event) => {
                    const value = event.target.value;
                    if (value === "__custom__") {
                      setCustomVoice(true);
                      if (isBaseVoice(editing.voice)) setEditing({ ...editing, voice: "" });
                    } else {
                      setCustomVoice(false);
                      setEditing({ ...editing, voice: value });
                    }
                  }}
                >
                  <option value="">请选择音色…</option>
                  {baseVoices.map((item) => (
                    <option key={item.voice} value={item.voice}>{item.label}</option>
                  ))}
                  <option value="__custom__">✎ 自定义音色…</option>
                </Select>
                {customVoice && (
                  <Input
                    value={editing.voice}
                    onChange={(event) => setEditing({ ...editing, voice: event.target.value })}
                    placeholder={config.tts.provider === "xfyun" ? "讯飞发音人 ID，如 x4_xxx" : "阿里云音色 ID，如 voice-xxxx（含复刻音色）"}
                  />
                )}
              </div>
              <div className="space-y-1.5">
                <Label>服务商</Label>
                <Input value={config.tts.provider === "xfyun" ? "讯飞" : "阿里云百炼 Qwen"} disabled />
              </div>
            </div>
            <div className="grid gap-3 md:grid-cols-4">
              <Field label="语速" type="number" value={String(editing.speech_rate ?? 1)} onChange={(value) => setEditing({ ...editing, speech_rate: Number(value) || 1 })} />
              <Field label="音量（0-100）" type="number" value={String(editing.volume ?? 70)} onChange={(value) => setEditing({ ...editing, volume: Math.max(0, Math.min(100, Number(value) || 70)) })} />
              <Field label="音调" type="number" value={String(editing.pitch_rate ?? 1)} onChange={(value) => setEditing({ ...editing, pitch_rate: Number(value) || 1 })} />
              <div className="space-y-1.5">
                <Label>继承模型</Label>
                <Input value={String(config.tts.settings?.model ?? editing.model)} disabled />
              </div>
            </div>
            <Textarea rows={2} value={editing.instructions ?? ""} onChange={(event) => setEditing({ ...editing, instructions: event.target.value })} placeholder="表达风格指令，例如：语气自信、有条理，适合攻辩。" className="font-sans" />
            <Textarea rows={2} value={editing.description ?? ""} onChange={(event) => setEditing({ ...editing, description: event.target.value })} placeholder="音色说明，仅用于管理端展示" className="font-sans" />
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex gap-4">
                <label className="flex items-center gap-2 text-sm"><Switch checked={editing.enabled} onCheckedChange={(value) => setEditing({ ...editing, enabled: value })} /> 启用</label>
                <label className="flex items-center gap-2 text-sm"><Switch checked={editing.is_default} onCheckedChange={(value) => setEditing({ ...editing, is_default: value })} /> 设为默认</label>
              </div>
              <div className="flex gap-2">
                <Button variant="outline" onClick={() => setEditing(null)}>取消</Button>
                <Button onClick={savePreset} loading={saving}><Save /> 保存音色</Button>
              </div>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function Field({ label, value, onChange, type = "text" }: { label: string; value: string; onChange: (value: string) => void; type?: string }) {
  return (
    <div className="space-y-1.5">
      <Label>{label}</Label>
      <Input type={type} value={value} onChange={(event) => onChange(event.target.value)} />
    </div>
  );
}

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
      for (const value of data) sum += (value - 128) ** 2;
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
    ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.type === "ready") void beginMic(ws);
      else if (message.type === "partial") setPartial(message.text || "");
      else if (message.type === "final") {
        setFinals((prev) => [...prev, message.text]);
        setPartial("");
      } else if (message.type === "done") {
        setDone(`识别完成 · ${message.chunk_count ?? 0} 段 · 延迟 ${message.latency_ms ?? 0}ms`);
        cleanup();
      } else if (message.type === "error") {
        toast(message.message || "ASR 流式错误", "error");
        cleanup();
      }
    };
    ws.onerror = () => toast("ASR 连接失败", "error");
    ws.onclose = () => cleanup();
  }

  async function beginMic(ws: WebSocket) {
    try {
      const stream = await startPcmStream((frame) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(frame);
      });
      streamRef.current = stream;
      meter(stream.analyser);
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
        <CardTitle className="flex items-center gap-2 text-sm"><Mic className="size-4" /> ASR 流式识别测试</CardTitle>
        <CardDescription>开始后边说边出字，停止结束本次会话。</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="h-3 w-full overflow-hidden rounded-full bg-muted">
          <div className="h-full rounded-full bg-success transition-all" style={{ width: `${level}%` }} />
        </div>
        <div className="flex gap-2">
          {recording ? (
            <Button variant="destructive" size="sm" onClick={stop}><Square /> 停止</Button>
          ) : (
            <Button variant="outline" size="sm" onClick={start}><Mic /> 开始流式识别</Button>
          )}
        </div>
        {(finals.length > 0 || partial || done) && (
          <div className="min-h-[3rem] space-y-1 rounded-md border border-border bg-muted/40 p-3 text-sm">
            {finals.map((text, index) => <span key={index} className="text-foreground">{text}</span>)}
            {partial && <span className="text-muted-foreground">{partial}</span>}
            {done && <p className="pt-1 text-xs text-success">{done}</p>}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function TtsTester({ matchId, presets }: { matchId: string; presets: VoicePreset[] }) {
  const toast = useToast();
  const [text, setText] = React.useState("人机辩论赛语音合成自检。");
  const [voicePresetId, setVoicePresetId] = React.useState("");
  const [probing, setProbing] = React.useState(false);
  const [result, setResult] = React.useState<string | null>(null);
  const [playing, setPlaying] = React.useState(false);

  React.useEffect(() => {
    if (!voicePresetId && presets.length) setVoicePresetId((presets.find((item) => item.is_default) ?? presets[0]).id);
  }, [presets, voicePresetId]);

  function testSpeaker() {
    try {
      const audioContext = new AudioContext();
      const osc = audioContext.createOscillator();
      const gain = audioContext.createGain();
      osc.type = "sine";
      osc.frequency.value = 660;
      gain.gain.setValueAtTime(0.0001, audioContext.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.25, audioContext.currentTime + 0.05);
      gain.gain.exponentialRampToValueAtTime(0.0001, audioContext.currentTime + 0.6);
      osc.connect(gain).connect(audioContext.destination);
      osc.start();
      osc.stop(audioContext.currentTime + 0.65);
      setPlaying(true);
      window.setTimeout(() => {
        setPlaying(false);
        void audioContext.close();
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
    let bytes = 0;
    const audioChunks: Uint8Array[] = [];
    let objectUrl: string | null = null;
    const selected = presets.find((item) => item.id === voicePresetId);
    const mime = selected?.response_format === "wav" ? "audio/wav" : selected?.response_format === "pcm" ? "audio/L16" : "audio/mpeg";
    const ws = new WebSocket(testWsUrl("tts", matchId));
    ws.onopen = () => ws.send(JSON.stringify({ text, voice_preset_id: voicePresetId }));
    ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.type === "ready") {
        setResult(`已连接 ${message.provider} · ${message.voice_preset_id || "默认音色"}`);
      } else if (message.type === "chunk") {
        chunks += 1;
        if (!firstChunkAt) {
          firstChunkAt = performance.now();
        }
        const chunk = base64ToBytes(message.audio_base64);
        bytes += chunk.byteLength;
        audioChunks.push(chunk);
        setResult(`流式接收中… 已收 ${chunks} 段 · ${bytes} bytes`);
      } else if (message.type === "done") {
        const first = firstChunkAt ? Math.round(firstChunkAt - startedAt) : message.latency_ms;
        if (!bytes) {
          setResult(`合成结束但没有收到音频分片 · ${message.mime_type}`);
          toast("TTS 未返回音频分片", "error");
        } else {
          const merged = new Uint8Array(bytes);
          let offset = 0;
          for (const chunk of audioChunks) {
            merged.set(chunk, offset);
            offset += chunk.byteLength;
          }
          const audio = new Audio();
          const blob = new Blob([merged.buffer as ArrayBuffer], { type: message.mime_type || mime });
          objectUrl = URL.createObjectURL(blob);
          audio.src = objectUrl;
          audio.onended = () => {
            if (objectUrl) URL.revokeObjectURL(objectUrl);
          };
          audio.onerror = () => {
            if (objectUrl) URL.revokeObjectURL(objectUrl);
            toast("浏览器无法播放返回的音频格式", "error");
          };
          void audio.play().catch((err) => {
            if (objectUrl) URL.revokeObjectURL(objectUrl);
            toast(err instanceof Error ? `播放失败：${err.message}` : "播放失败，请先点击测扬声器或检查浏览器权限", "error");
          });
          setResult(`合成完成并开始播放 · ${message.chunk_count} 段 · ${bytes} bytes · 首段 ${first}ms · 总 ${message.latency_ms}ms · ${message.mime_type}`);
          toast("TTS 合成完成，正在播放", "success");
        }
        setProbing(false);
        ws.close();
      } else if (message.type === "error") {
        toast(message.message || "TTS 流式失败", "error");
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
        <CardTitle className="flex items-center gap-2 text-sm"><Volume2 className="size-4" /> TTS 流式合成与播放测试</CardTitle>
        <CardDescription>选择已启用音色，边合成边播放。</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <Select value={voicePresetId} onChange={(event) => setVoicePresetId(event.target.value)} disabled={!presets.length}>
          {presets.map((preset) => <option key={preset.id} value={preset.id}>{preset.name} · {preset.voice}</option>)}
        </Select>
        <Textarea rows={2} value={text} onChange={(event) => setText(event.target.value)} className="font-sans" />
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
            <p className="flex items-center gap-1.5 text-success"><CheckCircle2 className="size-3.5" /> {result}</p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
