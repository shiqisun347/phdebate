import * as React from "react";
import {
  Play, Pause, SkipForward, RotateCcw, Square, AlertOctagon, Bot, Sparkles, MessageSquare,
  Award, UserCircle2, HelpCircle, RefreshCw, Hand, Send, Trophy, Megaphone, ChevronDown, Clock, Undo2,
} from "lucide-react";
import { Button, Card, CardContent, CardHeader, CardTitle, CardDescription, Badge, Select, Textarea, Input, Separator, Spinner } from "../ui/primitives";
import { Dialog, DialogHeader, DialogBody, DialogFooter } from "../ui/Dialog";
import { useToast } from "../lib/toast";
import { useAdminData } from "../lib/data";
import { useAction } from "../lib/actions";
import { post, put, sendXiaoqiCommand } from "../../api/client";
import { SCENE_LABELS, STATUS_LABELS, sideLabel } from "../lib/labels";
import type { Speaker, XiaoqiCommand } from "../../types/contracts";

const SCENE_STEPS: Array<{ scene: string; label: string }> = [
  { scene: "opening", label: "辩题介绍" },
  { scene: "teams", label: "阵容介绍" },
  { scene: "live", label: "比赛实况" },
  { scene: "xiaoqi_commentary", label: "小七观点" },
  { scene: "xiaoqi_result", label: "小七结果" },
  { scene: "judge_commentary", label: "评委点评" },
  { scene: "judge_result", label: "评委结果" },
  { scene: "audience_result", label: "学生结果" },
];

export function Control() {
  const { snapshot, matchId } = useAdminData();
  const { run, pending } = useAction();
  const toast = useToast();

  if (!snapshot) {
    return (
      <div className="flex items-center gap-2 py-20 text-muted-foreground">
        <Spinner /> 正在加载控场台…
      </div>
    );
  }

  const m = snapshot.match;
  const base = `/api/matches/${matchId}`;
  const setScene = (scene: string) => run(() => post(`${base}/screen/scene`, { scene }), { success: `已切换：${SCENE_LABELS[scene] ?? scene}` });

  return (
    <div className="space-y-5">
      {/* 顶部状态 + 生命周期控制 */}
      <Card>
        <CardContent className="space-y-4 p-5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={m.status === "running" ? "success" : m.status === "paused" ? "warning" : "muted"}>
              {STATUS_LABELS[m.status] ?? m.status}
            </Badge>
            <Badge variant="secondary">大屏：{SCENE_LABELS[m.screen_scene] ?? m.screen_scene}</Badge>
            <span className="ml-auto truncate text-sm text-muted-foreground">{m.title}</span>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button variant="success" loading={pending} onClick={() => run(async () => { await post(`${base}/start`); await post(`${base}/screen/scene`, { scene: "live" }); }, { success: "比赛已开始" })}>
              <Play /> 开始比赛
            </Button>
            <Button variant="outline" onClick={() => run(() => post(`${base}/pause`), { success: "已暂停" })}>
              <Pause /> 暂停
            </Button>
            <Button variant="outline" onClick={() => run(() => post(`${base}/resume`), { success: "已继续" })}>
              <Play /> 继续
            </Button>
            <Button variant="outline" onClick={() => run(() => post(`${base}/phases/next`), { success: "已进入下一阶段" })}>
              <SkipForward /> 下一阶段
            </Button>
            <Button
              variant="outline"
              onClick={() => {
                if (confirm("回退到上一阶段？用于撤销误操作。"))
                  run(() => post(`${base}/phases/${m.current_phase_id}/rollback`), { success: "已回退上一步" });
              }}
            >
              <Undo2 /> 回退上一步
            </Button>
            <Button variant="outline" onClick={() => { if (confirm("重置当前发言？")) run(() => post(`${base}/speeches/current/reset`), { success: "已重置当前发言" }); }}>
              <RotateCcw /> 重置当前发言
            </Button>
            <Button variant="outline" onClick={() => { if (confirm("重置整个比赛？将清空进度。")) run(() => post(`${base}/reset`), { success: "比赛已重置" }); }}>
              <Square /> 重置比赛
            </Button>
            <Button variant="destructive" className="ml-auto" onClick={() => { if (confirm("紧急停止？")) run(() => post(`${base}/emergency-stop`), { success: "已紧急停止" }); }}>
              <AlertOctagon /> 紧急停止
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* 大屏场景导航（10 步流程） */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm">大屏场景切换</CardTitle>
          <CardDescription>按现场流程推进主屏幕画面。</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap gap-2">
            {SCENE_STEPS.map((s, i) => (
              <button
                key={s.scene}
                onClick={() => setScene(s.scene)}
                className={`inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm transition-colors ${
                  m.screen_scene === s.scene
                    ? "border-primary bg-primary text-primary-foreground"
                    : "border-border bg-card text-foreground hover:bg-accent"
                }`}
              >
                <span className="text-xs opacity-70">{i + 1}</span> {s.label}
              </button>
            ))}
          </div>
        </CardContent>
      </Card>

      <CurrentPhaseCard />

      <div className="grid gap-5 lg:grid-cols-2">
        <AgentControlPanel />
        <XiaoqiControlPanel onSceneCommentary={() => setScene("xiaoqi_commentary")} onSceneResult={() => setScene("xiaoqi_result")} />
      </div>

      <ResultPanel />

      <DebateProcess />
    </div>
  );
}

function CurrentPhaseCard() {
  const { snapshot } = useAdminData();
  if (!snapshot) return null;
  const phase = snapshot.phases.find((p) => p.id === snapshot.match.current_phase_id);
  const clock = snapshot.clocks[0];
  const next = snapshot.next_speaker;
  return (
    <Card>
      <CardContent className="flex flex-wrap items-center gap-4 p-4">
        <div>
          <p className="text-xs text-muted-foreground">当前环节</p>
          <p className="font-semibold text-foreground">{phase?.name ?? "—"}</p>
        </div>
        {clock && (
          <div className="flex items-center gap-1.5 rounded-md bg-muted px-3 py-1.5">
            <Clock className="size-4 text-primary" />
            <span className="font-mono text-sm font-semibold">{Math.max(0, Math.round((clock.remaining_ms ?? 0) / 1000))}s</span>
            <Badge variant="muted">{clock.state}</Badge>
          </div>
        )}
        {next && (
          <div className="ml-auto text-right">
            <p className="text-xs text-muted-foreground">下一位发言</p>
            <p className="text-sm font-medium text-foreground">{next.label}</p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/* ----------------------------- AI 辩手控制 ----------------------------- */
function AgentControlPanel() {
  const { snapshot, matchId } = useAdminData();
  const { run } = useAction();
  const toast = useToast();
  const [fallback, setFallback] = React.useState<Speaker | null>(null);
  const agents = (snapshot?.speakers ?? []).filter((s) => s.speaker_type === "agent");
  const base = `/api/matches/${matchId}`;

  const activeSpeakerId = snapshot?.current_speech?.speaker_id ?? snapshot?.next_speaker?.speaker_id;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <Bot className="size-4" /> AI 辩手控制
        </CardTitle>
        <CardDescription>点击 AI 辩手进行自我介绍、重试、替代方案或中断。</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {agents.length === 0 && <p className="text-sm text-muted-foreground">本场没有 AI 辩手。</p>}
        {agents.map((a) => {
          const isTurn = activeSpeakerId === a.id;
          const st = snapshot?.agent_status.find((s) => s.speaker_id === a.id);
          return (
            <div key={a.id} className="rounded-lg border border-border p-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <div className="flex size-8 items-center justify-center rounded-md bg-primary/10 text-primary">
                    <Bot className="size-4" />
                  </div>
                  <div>
                    <p className="text-sm font-medium text-foreground">{a.name} <span className="text-xs text-muted-foreground">{sideLabel(a.side)}{a.seat}</span></p>
                    <p className="text-xs text-muted-foreground">{a.model_name || "未绑定"} {st ? `· ${st.status}` : ""}</p>
                  </div>
                </div>
                {isTurn && <Badge variant="success">当前发言</Badge>}
              </div>
              <div className="mt-2 flex flex-wrap gap-1.5">
                <Button size="sm" variant="outline" onClick={() => run(() => post(`${base}/speakers/${a.id}/start-agent-speaking`), { success: `${a.name} 开始发言` })}>
                  <UserCircle2 /> 自我介绍/发言
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    if (isTurn) run(() => post(`${base}/agent/${a.id}/retry`), { success: "已重新请求发言" });
                    else toast("并非该 agent 回答环节", "info");
                  }}
                >
                  <RefreshCw /> 重试
                </Button>
                <Button size="sm" variant="outline" onClick={() => setFallback(a)}>
                  <Hand /> 替代方案
                </Button>
                <Button size="sm" variant="ghost" className="text-destructive" onClick={() => run(() => post(`${base}/agent/${a.id}/interrupt`), { success: "已中断" })}>
                  中断
                </Button>
              </div>
            </div>
          );
        })}
      </CardContent>
      {fallback && <FallbackDialog speaker={fallback} onClose={() => setFallback(null)} />}
    </Card>
  );
}

function FallbackDialog({ speaker, onClose }: { speaker: Speaker; onClose: () => void }) {
  const { matchId, refresh } = useAdminData();
  const toast = useToast();
  const [text, setText] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  async function submit() {
    setSaving(true);
    try {
      await post(`/api/matches/${matchId}/agent/${speaker.id}/manual-input`, { content: text, reason: "fallback_single_answer" });
      await refresh();
      toast("已使用替代方案代答", "success");
      onClose();
    } catch (err) {
      toast(err instanceof Error ? err.message : "失败", "error");
    } finally {
      setSaving(false);
    }
  }
  return (
    <Dialog open onClose={onClose} size="md">
      <DialogHeader title={`替代方案 · ${speaker.name}`} description="紧急情况下，由控场人员代该 AI 辩手进行单次回答。" onClose={onClose} />
      <DialogBody>
        <Textarea rows={5} value={text} onChange={(e) => setText(e.target.value)} className="font-sans" placeholder="输入替代发言内容…" />
      </DialogBody>
      <DialogFooter>
        <Button variant="outline" onClick={onClose}>取消</Button>
        <Button onClick={submit} loading={saving} disabled={!text.trim()}>提交代答</Button>
      </DialogFooter>
    </Dialog>
  );
}

/* ----------------------------- 小七控制 ----------------------------- */
function XiaoqiControlPanel({ onSceneCommentary, onSceneResult }: { onSceneCommentary: () => void; onSceneResult: () => void }) {
  const { snapshot } = useAdminData();
  const toast = useToast();
  const [busy, setBusy] = React.useState<XiaoqiCommand | null>(null);
  const [customQ, setCustomQ] = React.useState("");

  async function send(command: XiaoqiCommand) {
    setBusy(command);
    try {
      const ctx = snapshot ? { debate_topic: snapshot.match.topic } : undefined;
      const r = await sendXiaoqiCommand({ command, question: command === "custom" ? customQ : undefined, context: ctx });
      toast(r.sent ? `已发送小七「${command}」命令` : `未发送：${r.reason}`, r.sent ? "success" : "info");
    } catch (err) {
      toast(err instanceof Error ? err.message : "发送失败", "error");
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <Sparkles className="size-4" /> 小七控制
        </CardTitle>
        <CardDescription>系统只负责发送命令，小七发音依赖其自身。</CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        <div className="grid grid-cols-2 gap-2">
          <Button variant="outline" loading={busy === "intro"} onClick={() => { onSceneCommentary(); send("intro"); }}>
            <UserCircle2 /> 自我介绍
          </Button>
          <Button variant="outline" loading={busy === "commentary"} onClick={() => { onSceneCommentary(); send("commentary"); }}>
            <MessageSquare /> 辩论点评
          </Button>
          <Button variant="outline" loading={busy === "result"} onClick={() => { onSceneResult(); send("result"); }}>
            <Award /> 辩论结果
          </Button>
        </div>
        <Separator />
        <div className="flex gap-2">
          <Input value={customQ} onChange={(e) => setCustomQ(e.target.value)} placeholder="自定义问题…" />
          <Button variant="outline" loading={busy === "custom"} disabled={!customQ.trim()} onClick={() => send("custom")}>
            <HelpCircle /> 发送
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

/* ----------------------------- 结果录入与公布 ----------------------------- */
function ResultPanel() {
  const { snapshot, matchId, refresh } = useAdminData();
  const toast = useToast();
  const [winner, setWinner] = React.useState<"affirmative" | "negative">("affirmative");
  const [best, setBest] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  if (!snapshot) return null;
  const base = `/api/matches/${matchId}`;
  const vs = snapshot.vote_state;

  async function act(fn: () => Promise<unknown>, msg: string) {
    setSaving(true);
    try {
      await fn();
      await refresh();
      toast(msg, "success");
    } catch (err) {
      toast(err instanceof Error ? err.message : "操作失败", "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <Trophy className="size-4" /> 结果录入与公布
        </CardTitle>
        <CardDescription>人工选择获胜方与最佳辩手并保存，再按流程公布。</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <p className="text-xs text-muted-foreground">获胜方</p>
            <Select value={winner} onChange={(e) => setWinner(e.target.value as "affirmative" | "negative")}>
              <option value="affirmative">正方</option>
              <option value="negative">反方</option>
            </Select>
          </div>
          <div className="space-y-1.5">
            <p className="text-xs text-muted-foreground">最佳辩手</p>
            <Select value={best} onChange={(e) => setBest(e.target.value)}>
              <option value="">请选择…</option>
              {snapshot.speakers.map((s) => (
                <option key={s.id} value={s.id}>
                  {sideLabel(s.side)}{s.seat} · {s.name}
                </option>
              ))}
            </Select>
          </div>
        </div>
        <Button
          onClick={() => {
            if (!best) return toast("请选择最佳辩手", "error");
            act(() => post(`${base}/votes`, { winner_side: winner, best_speaker_id: best }), "已保存获胜方与最佳辩手");
          }}
          loading={saving}
        >
          保存结果
        </Button>
        <Separator />
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="outline" onClick={() => act(() => post(`${base}/audience-votes/open`), "已开启学生投票")}>
            开启学生投票
          </Button>
          <Button size="sm" variant="outline" onClick={() => act(() => post(`${base}/audience-votes/close`), "已关闭学生投票")}>
            关闭学生投票
          </Button>
          <Button size="sm" variant="outline" onClick={() => act(() => post(`${base}/votes/publish`, { scope: "judge" }), "已公布评委结果")}>
            <Megaphone /> 公布评委结果
          </Button>
          <Button size="sm" variant="outline" onClick={() => act(() => post(`${base}/votes/publish`, { scope: "audience" }), "已公布学生结果")}>
            <Megaphone /> 公布学生结果
          </Button>
        </div>
        <div className="flex gap-2 text-xs text-muted-foreground">
          <span>当前获胜：{vs.winner_side ? sideLabel(vs.winner_side) : "—"}</span>
          <span>· 评委已公布：{vs.judge_published ? "是" : "否"}</span>
          <span>· 学生已公布：{vs.audience_published ? "是" : "否"}</span>
        </div>
      </CardContent>
    </Card>
  );
}

/* ----------------------------- 实时辩论过程（折叠在下方） ----------------------------- */
function DebateProcess() {
  const { snapshot } = useAdminData();
  const [open, setOpen] = React.useState(false);
  const segs = snapshot?.recent_transcript ?? [];
  return (
    <Card>
      <button className="flex w-full items-center justify-between p-4" onClick={() => setOpen((v) => !v)}>
        <span className="flex items-center gap-2 text-sm font-medium text-foreground">
          <MessageSquare className="size-4" /> 实时辩论过程（{segs.length}）
        </span>
        <ChevronDown className={`size-4 text-muted-foreground transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <CardContent className="max-h-96 space-y-2 overflow-y-auto pt-0">
          {segs.length === 0 && <p className="py-4 text-center text-sm text-muted-foreground">暂无发言记录</p>}
          {segs.map((s) => (
            <div key={s.id} className="rounded-md border border-border p-2.5">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-primary">{s.speaker_label}</span>
                <span className="text-xs text-muted-foreground">{s.is_final ? "终稿" : "实时"}</span>
              </div>
              <p className="mt-1 text-sm text-foreground">{s.text}</p>
            </div>
          ))}
        </CardContent>
      )}
    </Card>
  );
}
