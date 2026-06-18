import * as React from "react";
import { Users, Bot, User, Pencil } from "lucide-react";
import { Button, Card, CardContent, CardHeader, CardTitle, Badge, Input, Label, Select, Spinner } from "../ui/primitives";
import { Dialog, DialogHeader, DialogBody, DialogFooter } from "../ui/Dialog";
import { useToast } from "../lib/toast";
import { useAdminData } from "../lib/data";
import { patch, uploadSpeakerImage } from "../../api/client";
import type { Speaker } from "../../types/contracts";
import { resolveAvatar } from "../../state/avatar";

export function Debaters() {
  const { snapshot } = useAdminData();
  const [editing, setEditing] = React.useState<Speaker | null>(null);

  if (!snapshot) {
    return (
      <div className="flex items-center gap-2 py-20 text-muted-foreground">
        <Spinner /> 正在加载辩手…
      </div>
    );
  }

  const sides: Array<{ side: "affirmative" | "negative"; label: string; color: string }> = [
    { side: "affirmative", label: "正方", color: "text-blue-600" },
    { side: "negative", label: "反方", color: "text-rose-600" },
  ];
  const voiceById = new Map((snapshot.integration_config.voice_presets ?? []).map((preset) => [preset.id, preset]));

  return (
    <div className="space-y-5">
      <p className="text-sm text-muted-foreground">
        按当前赛制确定辩手数量与席位。在对应位置设置人类辩手或绑定 AI Agent。
      </p>
      <div className="grid gap-5 lg:grid-cols-2">
        {sides.map(({ side, label, color }) => {
          const speakers = snapshot.speakers.filter((s) => s.side === side).sort((a, b) => a.seat - b.seat);
          return (
            <Card key={side}>
              <CardHeader>
                <CardTitle className={`flex items-center gap-2 ${color}`}>
                  <Users className="size-4" /> {label}（{speakers.length} 人）
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-2">
                {speakers.map((s) => (
                  <div key={s.id} className="flex items-center justify-between gap-3 rounded-md border border-border p-3">
                    <div className="flex items-center gap-3">
                      <span className="flex size-8 items-center justify-center rounded-md bg-muted text-sm font-semibold text-foreground">
                        {s.seat}
                      </span>
                      <div>
                        <p className="text-sm font-medium text-foreground">{s.name}</p>
                        <p className="flex items-center gap-1 text-xs text-muted-foreground">
                          {s.speaker_type === "agent" ? <Bot className="size-3" /> : <User className="size-3" />}
                          {s.speaker_type === "agent" ? `AI · ${s.model_name || "未绑定"}` : "人类辩手"}
                        </p>
                        {s.speaker_type === "agent" && s.tts_voice_preset_id && (
                          <p className="text-xs text-muted-foreground">
                            音色：{voiceById.get(s.tts_voice_preset_id)?.name ?? s.tts_voice_preset_id}
                          </p>
                        )}
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <Badge variant={s.speaker_type === "agent" ? "default" : "secondary"}>
                        {s.speaker_type === "agent" ? "AI" : "人类"}
                      </Badge>
                      <Button size="icon" variant="ghost" onClick={() => setEditing(s)}>
                        <Pencil className="size-4" />
                      </Button>
                    </div>
                  </div>
                ))}
                {speakers.length === 0 && <p className="py-4 text-center text-sm text-muted-foreground">该方暂无席位</p>}
              </CardContent>
            </Card>
          );
        })}
      </div>

      {editing && <EditSpeaker speaker={editing} onClose={() => setEditing(null)} />}
    </div>
  );
}

function EditSpeaker({ speaker, onClose }: { speaker: Speaker; onClose: () => void }) {
  const { snapshot, matchId, refresh } = useAdminData();
  const toast = useToast();
  const [name, setName] = React.useState(speaker.name);
  const [type, setType] = React.useState<"human" | "agent">(speaker.speaker_type);
  const [agentConfigId, setAgentConfigId] = React.useState(speaker.agent_config_id ?? "");
  const [voicePresetId, setVoicePresetId] = React.useState(speaker.tts_voice_preset_id ?? "");
  const [saving, setSaving] = React.useState(false);
  const [uploading, setUploading] = React.useState(false);
  const fileRef = React.useRef<HTMLInputElement>(null);
  // Live speaker (so the preview updates after an upload refresh).
  const live = snapshot?.speakers.find((s) => s.id === speaker.id) ?? speaker;
  const configs = snapshot?.agent_configs ?? [];
  const ttsProvider = snapshot?.integration_config.tts.provider ?? "alicloud";
  const voicePresets = (snapshot?.integration_config.voice_presets ?? []).filter(
    (preset) => preset.enabled && preset.provider === ttsProvider
  );

  async function pickImage(file: File) {
    setUploading(true);
    try {
      await uploadSpeakerImage(matchId, speaker.id, file);
      await refresh();
      toast("形象图已更新", "success");
    } catch (err) {
      toast(err instanceof Error ? err.message : "上传失败", "error");
    } finally {
      setUploading(false);
    }
  }

  async function save() {
    if (type === "agent" && !agentConfigId) {
      toast("AI 辩手需绑定一个 Agent 配置", "error");
      return;
    }
    setSaving(true);
    try {
      const body: Record<string, unknown> = { name, speaker_type: type };
      if (type === "agent") {
        body.agent_config_id = agentConfigId;
        body.tts_voice_preset_id = voicePresetId || null;
      }
      await patch(`/api/matches/${matchId}/speakers/${speaker.id}`, body);
      await refresh();
      toast("已保存", "success");
      onClose();
    } catch (err) {
      toast(err instanceof Error ? err.message : "保存失败", "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open onClose={onClose} size="md">
      <DialogHeader title={`编辑辩手 · 第 ${speaker.seat} 席`} onClose={onClose} />
      <DialogBody>
        <div className="space-y-1.5">
          <Label>形象图（大屏阵容介绍 / 发言动效使用）</Label>
          <div className="flex items-center gap-3">
            <img
              src={resolveAvatar(live)}
              alt="辩手形象"
              className="size-16 shrink-0 rounded-lg border border-border object-cover"
            />
            <div className="space-y-1.5">
              <input
                ref={fileRef}
                type="file"
                accept="image/*"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) void pickImage(file);
                  e.target.value = "";
                }}
              />
              <Button size="sm" variant="outline" loading={uploading} onClick={() => fileRef.current?.click()}>
                {live.image_url ? "更换形象图" : "上传形象图"}
              </Button>
              <p className="text-xs text-muted-foreground">
                {live.image_url ? "已上传自定义形象。" : "未上传时大屏使用自动生成的默认形象。"}
              </p>
            </div>
          </div>
        </div>
        <div className="space-y-1.5">
          <Label>辩手姓名</Label>
          <Input value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="space-y-1.5">
          <Label>辩手类型</Label>
          <Select value={type} onChange={(e) => setType(e.target.value as "human" | "agent")}>
            <option value="human">人类辩手</option>
            <option value="agent">AI Agent 辩手</option>
          </Select>
        </div>
        {type === "agent" && (
          <>
            <div className="space-y-1.5">
              <Label>绑定 Agent 配置</Label>
              <Select value={agentConfigId} onChange={(e) => setAgentConfigId(e.target.value)}>
                <option value="">请选择…</option>
                {configs.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}（{c.model_name}）
                  </option>
                ))}
              </Select>
              {configs.length === 0 && <p className="text-xs text-warning">尚无 Agent 配置，请先到「Agent 管理」创建。</p>}
            </div>
            <div className="space-y-1.5">
              <Label>预设音色</Label>
              <Select value={voicePresetId} onChange={(e) => setVoicePresetId(e.target.value)}>
                <option value="">使用语音引擎默认音色</option>
                {voicePresets.map((preset) => (
                  <option key={preset.id} value={preset.id}>
                    {preset.name}（{preset.voice}）
                  </option>
                ))}
              </Select>
              {voicePresets.length === 0 && <p className="text-xs text-warning">当前 TTS 服务商暂无启用音色，请先到「语音引擎」维护。</p>}
            </div>
          </>
        )}
      </DialogBody>
      <DialogFooter>
        <Button variant="outline" onClick={onClose}>取消</Button>
        <Button onClick={save} loading={saving}>保存</Button>
      </DialogFooter>
    </Dialog>
  );
}
