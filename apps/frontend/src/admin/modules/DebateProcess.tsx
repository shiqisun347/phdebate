import * as React from "react";
import { MessagesSquare, RefreshCw, Pencil, Filter } from "lucide-react";
import { Button, Card, CardContent, Badge, Input, Textarea, Spinner, EmptyState } from "../ui/primitives";
import { Dialog, DialogHeader, DialogBody, DialogFooter } from "../ui/Dialog";
import { useToast } from "../lib/toast";
import { useAdminData } from "../lib/data";
import { patchSpeech } from "../../api/client";
import { sideLabel } from "../lib/labels";
import type { TranscriptSegment } from "../../types/contracts";

/**
 * 实时辩论过程：聊天框形式查看历史辩论，并可修正历史内容。
 * 修改经确认后通过 PATCH /speeches/{id} 同步到全局历史，后续 agent 请求基于修正后的内容。
 */
export function DebateProcess() {
  const { snapshot, matchId, refresh } = useAdminData();
  const toast = useToast();
  const [editing, setEditing] = React.useState<TranscriptSegment | null>(null);
  const [query, setQuery] = React.useState("");
  const [loading, setLoading] = React.useState(false);

  if (!snapshot) {
    return (
      <div className="flex items-center gap-2 py-20 text-muted-foreground">
        <Spinner /> 正在加载辩论过程…
      </div>
    );
  }

  // recent_transcript 为最新在前；这里按时间正序展示（聊天框习惯）。
  const segments = [...snapshot.recent_transcript].reverse().filter((s) => {
    if (!query.trim()) return true;
    const q = query.toLowerCase();
    return `${s.speaker_label} ${s.text}`.toLowerCase().includes(q);
  });

  async function reload() {
    setLoading(true);
    try {
      await refresh();
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex h-[calc(100vh-9rem)] flex-col gap-3">
      {/* 控制区 */}
      <Card>
        <CardContent className="flex flex-wrap items-center gap-2 p-3">
          <span className="flex items-center gap-2 text-sm font-medium text-foreground">
            <MessagesSquare className="size-4 text-primary" /> 实时辩论过程
          </span>
          <Badge variant="muted">{snapshot.recent_transcript.length} 段</Badge>
          <div className="relative ml-2 min-w-[180px] flex-1">
            <Filter className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索发言人 / 内容…" className="px-9" />
          </div>
          <Button variant="outline" size="sm" loading={loading} onClick={reload}>
            <RefreshCw /> 刷新
          </Button>
        </CardContent>
      </Card>

      {/* 聊天框 */}
      <Card className="flex-1 overflow-hidden">
        <CardContent className="h-full space-y-3 overflow-y-auto p-4">
          {segments.length === 0 ? (
            <EmptyState icon={<MessagesSquare />} title="暂无辩论记录" description="比赛开始后，发言内容会实时出现在这里。" />
          ) : (
            segments.map((s) => {
              const sideAff = s.speaker_label.startsWith("正方") || /aff/.test(s.speaker_id);
              return (
                <div key={s.id} className={`flex ${sideAff ? "justify-start" : "justify-end"}`}>
                  <div className={`max-w-[78%] rounded-lg border p-3 ${sideAff ? "border-blue-500/30 bg-blue-500/5" : "border-rose-500/30 bg-rose-500/5"}`}>
                    <div className="mb-1 flex items-center gap-2">
                      <span className="text-xs font-medium text-primary">{s.speaker_label}</span>
                      <Badge variant={s.is_final ? "secondary" : "muted"}>{s.is_final ? "终稿" : "实时"}</Badge>
                      {!s.valid && <Badge variant="destructive">已失效</Badge>}
                      <span className="text-[11px] text-muted-foreground">{s.source === "agent_text" ? "AI" : s.source === "human_asr" ? "转写" : "手动"}</span>
                      <Button size="icon" variant="ghost" className="ml-1 size-6" onClick={() => setEditing(s)} title="修正本段">
                        <Pencil className="size-3.5" />
                      </Button>
                    </div>
                    <p className="whitespace-pre-wrap text-sm text-foreground">{s.text || "（空）"}</p>
                  </div>
                </div>
              );
            })
          )}
        </CardContent>
      </Card>

      {editing && (
        <EditSegmentDialog
          segment={editing}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            await refresh();
            toast("已修改并同步到全局历史辩论", "success");
            setEditing(null);
          }}
          matchId={matchId}
        />
      )}
    </div>
  );
}

function EditSegmentDialog({
  segment, matchId, onClose, onSaved,
}: {
  segment: TranscriptSegment;
  matchId: string;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const toast = useToast();
  const [text, setText] = React.useState(segment.text);
  const [step, setStep] = React.useState<"edit" | "confirm">("edit");
  const [saving, setSaving] = React.useState(false);

  async function save() {
    setSaving(true);
    try {
      await patchSpeech(matchId, segment.speech_id, { text, reason: "host_correction" });
      await onSaved();
    } catch (err) {
      toast(err instanceof Error ? err.message : "修改失败", "error");
      setSaving(false);
    }
  }

  return (
    <Dialog open onClose={onClose} size="lg">
      <DialogHeader
        title={`修正辩论内容 · ${segment.speaker_label}`}
        description={step === "edit" ? "修改后需确认才会生效，并同步到全局历史辩论。" : undefined}
        onClose={onClose}
      />
      <DialogBody>
        {step === "edit" ? (
          <Textarea rows={8} value={text} onChange={(e) => setText(e.target.value)} className="font-sans" />
        ) : (
          <div className="space-y-3 text-sm">
            <p className="text-muted-foreground">确认将本段修改后，将同步到全局维护的历史辩论内容；后续发送给 agent 的请求也会基于修改后的内容。</p>
            <div className="rounded-md border border-border p-3">
              <p className="mb-1 text-xs text-muted-foreground">修改后内容</p>
              <p className="whitespace-pre-wrap text-foreground">{text || "（空）"}</p>
            </div>
          </div>
        )}
      </DialogBody>
      <DialogFooter>
        {step === "edit" ? (
          <>
            <Button variant="outline" onClick={onClose}>取消</Button>
            <Button onClick={() => setStep("confirm")} disabled={text === segment.text}>下一步</Button>
          </>
        ) : (
          <>
            <Button variant="outline" onClick={() => setStep("edit")}>返回修改</Button>
            <Button onClick={save} loading={saving}>确认修改并同步</Button>
          </>
        )}
      </DialogFooter>
    </Dialog>
  );
}
