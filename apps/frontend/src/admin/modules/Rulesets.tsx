import * as React from "react";
import { ListChecks, Plus, Pencil, Trash2, Sparkles, Copy, Check } from "lucide-react";
import { Button, Card, CardContent, Badge, Input, Label, Textarea, Spinner, EmptyState, Separator } from "../ui/primitives";
import { Dialog, DialogHeader, DialogBody, DialogFooter } from "../ui/Dialog";
import { FlowDiagram } from "../ui/FlowDiagram";
import { useToast } from "../lib/toast";
import { listRulesets, createRuleset, updateRuleset, deleteRuleset, generateRulesetFlow } from "../../api/client";
import type { Ruleset, RulesetFlowNode } from "../../types/contracts";

export function Rulesets() {
  const toast = useToast();
  const [list, setList] = React.useState<Ruleset[] | null>(null);
  const [template, setTemplate] = React.useState("");
  const [editing, setEditing] = React.useState<Ruleset | null>(null);
  const [creating, setCreating] = React.useState(false);

  const load = React.useCallback(async () => {
    try {
      const r = await listRulesets();
      setList(r.rulesets);
      setTemplate(r.flow_template);
    } catch (err) {
      toast(err instanceof Error ? err.message : "加载失败", "error");
    }
  }, [toast]);

  React.useEffect(() => {
    void load();
  }, [load]);

  if (!list) {
    return (
      <div className="flex items-center gap-2 py-20 text-muted-foreground">
        <Spinner /> 正在加载赛制规则…
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">预设赛制规则库。保存当前比赛正在使用的赛制时，会同步到当前比赛的阶段时长。</p>
        <Button onClick={() => setCreating(true)}>
          <Plus /> 新增赛制
        </Button>
      </div>

      {list.length === 0 ? (
        <EmptyState icon={<ListChecks />} title="还没有赛制规则" action={<Button onClick={() => setCreating(true)}><Plus /> 新增赛制</Button>} />
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {list.map((r) => (
            <Card key={r.id}>
              <CardContent className="space-y-3 p-5">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="font-semibold text-foreground">{r.name}</p>
                    <p className="text-sm text-muted-foreground">{r.summary || "无简介"}</p>
                  </div>
                  <Badge variant="secondary">{r.flow.length} 环节</Badge>
                </div>
                <div className="max-h-64 overflow-y-auto rounded-md border border-border p-3">
                  <FlowDiagram nodes={r.flow} compact />
                </div>
                <Separator />
                <div className="flex justify-end gap-2">
                  <Button size="sm" variant="ghost" onClick={() => setEditing(r)}>
                    <Pencil /> 编辑
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="text-destructive hover:bg-destructive/10"
                    onClick={async () => {
                      if (confirm(`确认删除赛制「${r.name}」？`)) {
                        try {
                          await deleteRuleset(r.id);
                          toast("已删除", "success");
                          await load();
                        } catch (err) {
                          toast(err instanceof Error ? err.message : "删除失败", "error");
                        }
                      }
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
        <RulesetDialog
          initial={editing}
          defaultTemplate={template}
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          onSaved={async () => {
            setCreating(false);
            setEditing(null);
            await load();
          }}
        />
      )}
    </div>
  );
}

function RulesetDialog({
  initial,
  defaultTemplate,
  onClose,
  onSaved,
}: {
  initial: Ruleset | null;
  defaultTemplate: string;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const toast = useToast();
  const [name, setName] = React.useState(initial?.name ?? "");
  const [summary, setSummary] = React.useState(initial?.summary ?? "");
  const [tpl, setTpl] = React.useState(initial?.template || defaultTemplate);
  const [flow, setFlow] = React.useState<RulesetFlowNode[]>(initial?.flow ?? []);
  const [generating, setGenerating] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [warnings, setWarnings] = React.useState<string[]>([]);
  const [copied, setCopied] = React.useState(false);

  async function generate() {
    setGenerating(true);
    try {
      const r = await generateRulesetFlow(tpl, true);
      setFlow(r.nodes);
      setWarnings(r.warnings);
      if (r.normalized_template) setTpl(r.normalized_template);
      toast(r.ai_used ? "已用 AI 生成结构化流程" : `已生成 ${r.nodes.length} 个环节`, "success");
    } catch (err) {
      toast(err instanceof Error ? err.message : "生成失败", "error");
    } finally {
      setGenerating(false);
    }
  }

  async function save() {
    if (!name.trim()) {
      toast("请填写赛制名称", "error");
      return;
    }
    setSaving(true);
    try {
      const body = { name, summary, template: tpl, flow };
      const saved = initial ? await updateRuleset(initial.id, body) : await createRuleset(body);
      const applied = (saved as Ruleset & { applied_current_match?: { applied?: boolean; updated_phase_count?: number } }).applied_current_match;
      toast(
        applied?.applied
          ? `已保存，并同步当前比赛 ${applied.updated_phase_count ?? 0} 个环节`
          : initial ? "已保存" : "已创建",
        "success",
      );
      await onSaved();
    } catch (err) {
      toast(err instanceof Error ? err.message : "保存失败", "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open onClose={onClose} size="xl">
      <DialogHeader title={initial ? "编辑赛制规则" : "新增赛制规则"} description="编辑流程模板后点击「生成流程」，确认流程图无误再保存。" onClose={onClose} />
      <DialogBody>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>赛制名称 <span className="text-destructive">*</span></Label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="如：标准 4v4 辩论赛制" />
          </div>
          <div className="space-y-1.5">
            <Label>赛制简介</Label>
            <Input value={summary} onChange={(e) => setSummary(e.target.value)} placeholder="一句话描述该赛制" />
          </div>
        </div>

        <div className="grid gap-4 lg:grid-cols-2">
          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <Label>流程模板（可复制后修改）</Label>
              <button
                className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                onClick={() => {
                  void navigator.clipboard.writeText(tpl);
                  setCopied(true);
                  window.setTimeout(() => setCopied(false), 1500);
                }}
              >
                {copied ? <Check className="size-3" /> : <Copy className="size-3" />} 复制
              </button>
            </div>
            <Textarea rows={14} value={tpl} onChange={(e) => setTpl(e.target.value)} className="text-xs" />
            <Button variant="outline" onClick={generate} loading={generating} className="w-full">
              <Sparkles /> 生成流程图
            </Button>
          </div>
          <div className="space-y-1.5">
            <Label>程序流程图预览</Label>
            <div className="max-h-[22rem] overflow-y-auto rounded-md border border-border p-3">
              <FlowDiagram nodes={flow} />
            </div>
            {warnings.length > 0 && (
              <div className="rounded-md bg-warning/10 p-2 text-xs text-warning">
                {warnings.map((w, i) => (
                  <p key={i}>· {w}</p>
                ))}
              </div>
            )}
          </div>
        </div>
      </DialogBody>
      <DialogFooter>
        <Button variant="outline" onClick={onClose}>取消</Button>
        <Button onClick={save} loading={saving} disabled={!name.trim()}>保存赛制</Button>
      </DialogFooter>
    </Dialog>
  );
}
