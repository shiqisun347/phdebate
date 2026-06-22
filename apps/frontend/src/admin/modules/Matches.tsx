import * as React from "react";
import { Trophy, Plus, Trash2, Check, ArrowRight, ArrowLeft, Pencil, Star, Type, Image as ImageIcon, Upload } from "lucide-react";
import { Button, Card, CardContent, Badge, Input, Label, Spinner, EmptyState, Separator, Switch } from "../ui/primitives";
import { Dialog, DialogHeader, DialogBody, DialogFooter } from "../ui/Dialog";
import { FlowDiagram } from "../ui/FlowDiagram";
import { useToast } from "../lib/toast";
import { useAdminData } from "../lib/data";
import { listMatches, switchMatch, deleteMatch, post, patch, listRulesets, uploadMatchImage } from "../../api/client";
import { STATUS_LABELS } from "../lib/labels";
import type { BrandDisplay, MatchListEntry, Ruleset } from "../../types/contracts";

export function Matches() {
  const { matchList, refresh, refreshList, snapshot } = useAdminData();
  const toast = useToast();
  const [wizard, setWizard] = React.useState(false);
  const [editBase, setEditBase] = React.useState(false);

  const matches = matchList?.matches ?? [];

  async function doSwitch(id: string) {
    try {
      await switchMatch(id);
      await Promise.all([refresh(), refreshList()]);
      toast("已切换当前比赛", "success");
    } catch (err) {
      toast(err instanceof Error ? err.message : "切换失败", "error");
    }
  }
  async function doDelete(m: MatchListEntry) {
    const extra = m.active ? "（这是当前比赛，删除后将切换到其它比赛；若没有其它比赛则回到空白起步）" : "";
    if (!confirm(`确认删除比赛「${m.title || m.id}」？该操作不可恢复。${extra}`)) return;
    try {
      await deleteMatch(m.id);
      await Promise.all([refresh(), refreshList()]);
      toast("已删除", "success");
    } catch (err) {
      toast(err instanceof Error ? err.message : "删除失败", "error");
    }
  }

  if (!matchList) {
    return (
      <div className="flex items-center gap-2 py-20 text-muted-foreground">
        <Spinner /> 正在加载比赛列表…
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">管理所有比赛。新建比赛通过多步向导完成基础信息与赛制选择。</p>
        <Button onClick={() => setWizard(true)}>
          <Plus /> 新建比赛
        </Button>
      </div>

      {matches.length === 0 ? (
        <EmptyState icon={<Trophy />} title="还没有比赛" action={<Button onClick={() => setWizard(true)}><Plus /> 新建比赛</Button>} />
      ) : (
        <div className="space-y-2">
          {matches.map((m) => (
            <Card key={m.id}>
              <CardContent className="flex flex-wrap items-center justify-between gap-3 p-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <p className="truncate font-semibold text-foreground">{m.title || m.id}</p>
                    {m.active && <Badge variant="success">当前比赛</Badge>}
                    <Badge variant="muted">{STATUS_LABELS[m.status] ?? m.status}</Badge>
                  </div>
                  <p className="truncate text-sm text-muted-foreground">{m.topic || "未设置辩题"}</p>
                </div>
                <div className="flex gap-2">
                  {m.active ? (
                    <Button size="sm" variant="outline" onClick={() => setEditBase(true)}>
                      <Pencil /> 编辑信息
                    </Button>
                  ) : (
                    <Button size="sm" variant="outline" onClick={() => doSwitch(m.id)}>
                      切换为当前 <ArrowRight />
                    </Button>
                  )}
                  <Button
                    size="sm"
                    variant="ghost"
                    className="text-destructive hover:bg-destructive/10"
                    title={m.active ? "删除当前比赛（删除后切到其它比赛或回到空白起步）" : "删除该比赛"}
                    onClick={() => doDelete(m)}
                  >
                    <Trash2 />
                  </Button>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {wizard && (
        <CreateWizard
          previousActiveId={matchList.active_match_id}
          onClose={() => setWizard(false)}
          onDone={async () => {
            setWizard(false);
            await Promise.all([refresh(), refreshList()]);
          }}
        />
      )}
      {editBase && snapshot && (
        <EditBaseDialog
          onClose={() => setEditBase(false)}
          onSaved={async () => {
            setEditBase(false);
            await Promise.all([refresh(), refreshList()]);
          }}
        />
      )}
    </div>
  );
}

/* --------------------------- 新建比赛向导 --------------------------- */
type Base = {
  title: string;
  topic: string;
  organizer: string;
  affirmative_position: string;
  negative_position: string;
  venue: string;
};
const EMPTY_BASE: Base = { title: "", topic: "", organizer: "", affirmative_position: "正方", negative_position: "反方", venue: "" };

function CreateWizard({ previousActiveId, onClose, onDone }: { previousActiveId: string; onClose: () => void; onDone: () => Promise<void> }) {
  const toast = useToast();
  const [step, setStep] = React.useState(0);
  const [base, setBase] = React.useState<Base>(EMPTY_BASE);
  const [rulesets, setRulesets] = React.useState<Ruleset[]>([]);
  const [rulesetId, setRulesetId] = React.useState<string>("");
  const [keepActive, setKeepActive] = React.useState(true);
  const [saving, setSaving] = React.useState(false);

  React.useEffect(() => {
    listRulesets().then((r) => {
      setRulesets(r.rulesets);
      if (r.rulesets[0]) setRulesetId(r.rulesets[0].id);
    });
  }, []);

  const setB = <K extends keyof Base>(k: K, v: Base[K]) => setBase((p) => ({ ...p, [k]: v }));
  const selectedRuleset = rulesets.find((r) => r.id === rulesetId);
  const canNext = step === 0 ? base.title.trim() && base.topic.trim() : step === 1 ? !!rulesetId : true;

  async function create() {
    setSaving(true);
    try {
      await post("/api/matches", { ...base, ruleset_id: rulesetId });
      if (!keepActive && previousActiveId) {
        await switchMatch(previousActiveId);
      }
      toast(keepActive ? "已创建并切换为当前比赛" : "已创建比赛", "success");
      await onDone();
    } catch (err) {
      toast(err instanceof Error ? err.message : "创建失败", "error");
    } finally {
      setSaving(false);
    }
  }

  const steps = ["基础信息", "选择赛制", "完成创建"];

  return (
    <Dialog open onClose={onClose} size="xl">
      <DialogHeader title="新建比赛" onClose={onClose} />
      <DialogBody>
        <div className="mb-2 flex items-center gap-2">
          {steps.map((s, i) => (
            <React.Fragment key={s}>
              <div className="flex items-center gap-2">
                <span
                  className={`flex size-7 items-center justify-center rounded-full text-xs font-semibold ${
                    i < step ? "bg-success text-success-foreground" : i === step ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground"
                  }`}
                >
                  {i < step ? <Check className="size-3.5" /> : i + 1}
                </span>
                <span className={`text-sm ${i === step ? "font-medium text-foreground" : "text-muted-foreground"}`}>{s}</span>
              </div>
              {i < steps.length - 1 && <div className="h-px flex-1 bg-border" />}
            </React.Fragment>
          ))}
        </div>
        <Separator />

        {step === 0 && (
          <div className="grid gap-4 pt-1 sm:grid-cols-2">
            <Field label="比赛名称" required hint="显示于大屏左上角">
              <Input value={base.title} onChange={(e) => setB("title", e.target.value)} placeholder="如：第一届人机辩论邀请赛" />
            </Field>
            <Field label="主办机构" hint="显示于大屏右上角">
              <Input value={base.organizer} onChange={(e) => setB("organizer", e.target.value)} placeholder="如：XX 大学辩论队" />
            </Field>
            <Field label="辩题" required>
              <Input value={base.topic} onChange={(e) => setB("topic", e.target.value)} placeholder="如：AI 时代应培养编程思维 / 提问思维" />
            </Field>
            <Field label="场地">
              <Input value={base.venue} onChange={(e) => setB("venue", e.target.value)} placeholder="可选" />
            </Field>
            <Field label="正方立场">
              <Input value={base.affirmative_position} onChange={(e) => setB("affirmative_position", e.target.value)} />
            </Field>
            <Field label="反方立场">
              <Input value={base.negative_position} onChange={(e) => setB("negative_position", e.target.value)} />
            </Field>
          </div>
        )}

        {step === 1 && (
          <div className="space-y-3 pt-1">
            <p className="text-sm text-muted-foreground">赛制规则只能从预设中选择，不可自定义。流程将作为本场比赛的预设流程。</p>
            {rulesets.length === 0 ? (
              <EmptyState icon={<Trophy />} title="暂无可用赛制" description="请先到「赛制规则」创建一个赛制。" />
            ) : (
              <div className="grid gap-3 lg:grid-cols-2">
                {rulesets.map((r) => (
                  <button
                    key={r.id}
                    onClick={() => setRulesetId(r.id)}
                    className={`rounded-lg border p-3 text-left transition-colors ${
                      rulesetId === r.id ? "border-primary bg-primary/5 ring-1 ring-primary" : "border-border hover:border-primary/40"
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <p className="font-medium text-foreground">{r.name}</p>
                      {rulesetId === r.id && <Check className="size-4 text-primary" />}
                    </div>
                    <p className="text-xs text-muted-foreground">{r.summary}</p>
                    <Badge variant="secondary" className="mt-1">{r.flow.length} 环节</Badge>
                  </button>
                ))}
              </div>
            )}
            {selectedRuleset && (
              <div className="max-h-60 overflow-y-auto rounded-md border border-border p-3">
                <FlowDiagram nodes={selectedRuleset.flow} compact />
              </div>
            )}
          </div>
        )}

        {step === 2 && (
          <div className="space-y-3 pt-1">
            <div className="rounded-lg border border-border p-4 text-sm">
              <Row k="比赛名称" v={base.title} />
              <Row k="辩题" v={base.topic} />
              <Row k="主办机构" v={base.organizer || "—"} />
              <Row k="赛制" v={selectedRuleset?.name || "—"} />
              <Row k="流程环节" v={`${selectedRuleset?.flow.length ?? 0} 个`} />
            </div>
            <label className="flex items-center gap-2.5">
              <Switch checked={keepActive} onCheckedChange={setKeepActive} />
              <span className="text-sm text-foreground">创建后切换为当前比赛</span>
            </label>
          </div>
        )}
      </DialogBody>
      <DialogFooter>
        {step > 0 && (
          <Button variant="outline" onClick={() => setStep((s) => s - 1)} className="mr-auto">
            <ArrowLeft /> 上一步
          </Button>
        )}
        <Button variant="outline" onClick={onClose}>取消</Button>
        {step < 2 ? (
          <Button disabled={!canNext} onClick={() => setStep((s) => s + 1)}>
            下一步 <ArrowRight />
          </Button>
        ) : (
          <Button onClick={create} loading={saving} disabled={!rulesetId}>
            <Star /> 创建比赛
          </Button>
        )}
      </DialogFooter>
    </Dialog>
  );
}

function EditBaseDialog({ onClose, onSaved }: { onClose: () => void; onSaved: () => Promise<void> }) {
  const { snapshot, matchId, refresh } = useAdminData();
  const toast = useToast();
  const m = snapshot!.match;
  const [base, setBase] = React.useState<Base>({
    title: m.title,
    topic: m.topic,
    organizer: m.organizer,
    affirmative_position: m.affirmative_position,
    negative_position: m.negative_position,
    venue: m.venue,
  });
  const [organizerDisplay, setOrganizerDisplay] = React.useState<BrandDisplay>(m.organizer_display ?? "text");
  const affTeam = snapshot!.teams.find((t) => t.side === "affirmative");
  const negTeam = snapshot!.teams.find((t) => t.side === "negative");
  const [affTeamName, setAffTeamName] = React.useState(affTeam?.name ?? "");
  const [negTeamName, setNegTeamName] = React.useState(negTeam?.name ?? "");
  const [saving, setSaving] = React.useState(false);
  const setB = <K extends keyof Base>(k: K, v: Base[K]) => setBase((p) => ({ ...p, [k]: v }));

  async function save() {
    setSaving(true);
    try {
      // 比赛名称：文本与 logo 同时生效（logo 经上传/移除独立保存）；title_image_url 非空时大屏显示 logo+文字。
      await patch(`/api/matches/${matchId}`, { ...base, title_display: m.title_image_url ? "image" : "text", organizer_display: organizerDisplay });
      // 战队名字（与立场分开）：仅在改动时更新对应战队。
      if (affTeam && affTeamName.trim() && affTeamName.trim() !== affTeam.name) {
        await patch(`/api/matches/${matchId}/teams/${affTeam.id}`, { name: affTeamName.trim() });
      }
      if (negTeam && negTeamName.trim() && negTeamName.trim() !== negTeam.name) {
        await patch(`/api/matches/${matchId}/teams/${negTeam.id}`, { name: negTeamName.trim() });
      }
      toast("已保存", "success");
      await onSaved();
    } catch (err) {
      toast(err instanceof Error ? err.message : "保存失败", "error");
    } finally {
      setSaving(false);
    }
  }

  async function uploadImage(kind: "title" | "organizer", file: File) {
    try {
      await uploadMatchImage(matchId, kind, file);
      if (kind === "organizer") setOrganizerDisplay("image");
      await refresh();
      toast("图片已上传", "success");
    } catch (err) {
      toast(err instanceof Error ? err.message : "上传失败", "error");
    }
  }

  async function removeTitleLogo() {
    try {
      await patch(`/api/matches/${matchId}`, { title_image_url: "", title_display: "text" });
      await refresh();
      toast("已移除 logo", "success");
    } catch (err) {
      toast(err instanceof Error ? err.message : "移除失败", "error");
    }
  }

  return (
    <Dialog open onClose={onClose} size="lg">
      <DialogHeader title="编辑比赛基础信息" description="仅当前比赛可编辑，保存后实时同步到大屏" onClose={onClose} />
      <DialogBody>
        <div className="grid gap-4 sm:grid-cols-2">
          <TitleLogoField
            label="比赛名称"
            hint="名称与 logo 可同时设置，大屏左上角 logo 显示在名称左侧（任一留空则只显示另一个）"
            text={base.title}
            onTextChange={(v) => setB("title", v)}
            logoUrl={m.title_image_url}
            onUpload={(file) => uploadImage("title", file)}
            onRemove={removeTitleLogo}
          />
          <BrandField
            label="主办机构"
            hint="显示于大屏右上角"
            mode={organizerDisplay}
            onModeChange={setOrganizerDisplay}
            text={base.organizer}
            onTextChange={(v) => setB("organizer", v)}
            imageUrl={m.organizer_image_url}
            onUpload={(file) => uploadImage("organizer", file)}
          />
          <Field label="辩题"><Input value={base.topic} onChange={(e) => setB("topic", e.target.value)} /></Field>
          <Field label="场地"><Input value={base.venue} onChange={(e) => setB("venue", e.target.value)} /></Field>
          <Field label="正方战队名" hint="同步到大屏正方战队名"><Input value={affTeamName} onChange={(e) => setAffTeamName(e.target.value)} placeholder="如：智码战队" /></Field>
          <Field label="反方战队名" hint="同步到大屏反方战队名"><Input value={negTeamName} onChange={(e) => setNegTeamName(e.target.value)} placeholder="如：问道战队" /></Field>
          <Field label="正方立场" hint="同步到大屏正方立场"><Input value={base.affirmative_position} onChange={(e) => setB("affirmative_position", e.target.value)} /></Field>
          <Field label="反方立场" hint="同步到大屏反方立场"><Input value={base.negative_position} onChange={(e) => setB("negative_position", e.target.value)} /></Field>
        </div>
      </DialogBody>
      <DialogFooter>
        <Button variant="outline" onClick={onClose}>取消</Button>
        <Button onClick={save} loading={saving}>保存</Button>
      </DialogFooter>
    </Dialog>
  );
}

function BrandField({
  label,
  hint,
  mode,
  onModeChange,
  text,
  onTextChange,
  imageUrl,
  onUpload,
}: {
  label: string;
  hint?: string;
  mode: BrandDisplay;
  onModeChange: (mode: BrandDisplay) => void;
  text: string;
  onTextChange: (value: string) => void;
  imageUrl?: string;
  onUpload: (file: File) => void | Promise<void>;
}) {
  const fileRef = React.useRef<HTMLInputElement>(null);
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <Label>{label}</Label>
        <div className="inline-flex overflow-hidden rounded-md border border-border text-xs">
          <button
            type="button"
            onClick={() => onModeChange("text")}
            className={`flex items-center gap-1 px-2 py-1 ${mode === "text" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"}`}
          >
            <Type className="size-3" /> 文本
          </button>
          <button
            type="button"
            onClick={() => onModeChange("image")}
            className={`flex items-center gap-1 px-2 py-1 ${mode === "image" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"}`}
          >
            <ImageIcon className="size-3" /> 图片
          </button>
        </div>
      </div>
      {mode === "text" ? (
        <Input value={text} onChange={(e) => onTextChange(e.target.value)} />
      ) : (
        <div className="space-y-2 rounded-md border border-border p-2">
          {imageUrl ? (
            <img src={imageUrl} alt={label} className="max-h-16 rounded object-contain" />
          ) : (
            <p className="text-xs text-muted-foreground">尚未上传图片，将回退为文本显示。</p>
          )}
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void onUpload(file);
              e.target.value = "";
            }}
          />
          <Button size="sm" variant="outline" onClick={() => fileRef.current?.click()}>
            <Upload /> {imageUrl ? "更换图片" : "上传图片"}
          </Button>
        </div>
      )}
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

/** 比赛名称：文本 + logo 同时可设置（不是二选一）；大屏上 logo 显示在文字左边。 */
function TitleLogoField({
  label,
  hint,
  text,
  onTextChange,
  logoUrl,
  onUpload,
  onRemove,
}: {
  label: string;
  hint?: string;
  text: string;
  onTextChange: (value: string) => void;
  logoUrl?: string;
  onUpload: (file: File) => void | Promise<void>;
  onRemove: () => void | Promise<void>;
}) {
  const fileRef = React.useRef<HTMLInputElement>(null);
  return (
    <div className="space-y-1.5">
      <Label>{label}</Label>
      <Input value={text} onChange={(e) => onTextChange(e.target.value)} placeholder="比赛名称（可与 logo 同时显示）" />
      <div className="flex items-center gap-3 rounded-md border border-border p-2">
        {logoUrl ? (
          <img src={logoUrl} alt={`${label} logo`} className="max-h-12 rounded object-contain" />
        ) : (
          <span className="text-xs text-muted-foreground">未设置 logo（可选，显示在名称左侧）</span>
        )}
        <div className="ml-auto flex shrink-0 gap-2">
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void onUpload(file);
              e.target.value = "";
            }}
          />
          <Button size="sm" variant="outline" onClick={() => fileRef.current?.click()}>
            <Upload /> {logoUrl ? "更换 logo" : "上传 logo"}
          </Button>
          {logoUrl && (
            <Button size="sm" variant="outline" onClick={() => void onRemove()}>
              移除
            </Button>
          )}
        </div>
      </div>
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

function Field({ label, required, hint, children }: { label: string; required?: boolean; hint?: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <Label>{label}{required && <span className="ml-0.5 text-destructive">*</span>}</Label>
      {children}
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex justify-between py-1">
      <span className="text-muted-foreground">{k}</span>
      <span className="font-medium text-foreground">{v}</span>
    </div>
  );
}
