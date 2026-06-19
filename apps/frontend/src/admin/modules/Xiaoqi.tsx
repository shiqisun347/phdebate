import * as React from "react";
import { Sparkles, Save, Send, ImageIcon, MessageSquare, Award, HelpCircle, UserCircle2, Upload } from "lucide-react";
import { Button, Card, CardContent, CardHeader, CardTitle, CardDescription, Input, Label, Textarea, Switch, Select, Badge, Spinner, Separator } from "../ui/primitives";
import { useToast } from "../lib/toast";
import { useAdminData } from "../lib/data";
import { getXiaoqi, updateXiaoqi, sendXiaoqiCommand, pushXiaoqiMatchRecord } from "../../api/client";
import type { XiaoqiConfig, XiaoqiCommand } from "../../types/contracts";

const COMMANDS: Array<{ key: XiaoqiCommand; label: string; icon: typeof MessageSquare }> = [
  { key: "intro", label: "自我介绍", icon: UserCircle2 },
  { key: "commentary", label: "评价辩论", icon: MessageSquare },
  { key: "result", label: "给出结果", icon: Award },
  { key: "custom", label: "自定义问题", icon: HelpCircle },
];

export function Xiaoqi() {
  const toast = useToast();
  const { matchId, snapshot } = useAdminData();
  const [cfg, setCfg] = React.useState<XiaoqiConfig | null>(null);
  const [saving, setSaving] = React.useState(false);
  const [tplText, setTplText] = React.useState("");
  const [tplError, setTplError] = React.useState<string | null>(null);
  const [testing, setTesting] = React.useState<XiaoqiCommand | null>(null);
  const [pushing, setPushing] = React.useState(false);
  const [customQ, setCustomQ] = React.useState("");
  const [result, setResult] = React.useState<string | null>(null);
  const hasMatch = Boolean(snapshot?.match.id);

  const load = React.useCallback(async () => {
    try {
      const c = await getXiaoqi();
      setCfg(c);
      setTplText(JSON.stringify(c.request_template, null, 2));
    } catch (err) {
      toast(err instanceof Error ? err.message : "加载失败", "error");
    }
  }, [toast]);

  React.useEffect(() => {
    void load();
  }, [load]);

  if (!cfg) {
    return (
      <div className="flex items-center gap-2 py-20 text-muted-foreground">
        <Spinner /> 正在加载小七配置…
      </div>
    );
  }

  const set = <K extends keyof XiaoqiConfig>(k: K, v: XiaoqiConfig[K]) => setCfg((p) => (p ? { ...p, [k]: v } : p));
  const setPrompt = (k: XiaoqiCommand, v: string) => setCfg((p) => (p ? { ...p, prompts: { ...p.prompts, [k]: v } } : p));

  async function save() {
    let request_template: Record<string, unknown> | undefined;
    if (tplText.trim()) {
      try {
        request_template = JSON.parse(tplText);
        setTplError(null);
      } catch {
        setTplError("请求体不是合法 JSON");
        toast("请求体 JSON 格式错误", "error");
        return;
      }
    }
    setSaving(true);
    try {
      const next = await updateXiaoqi({ ...cfg!, request_template });
      setCfg(next);
      setTplText(JSON.stringify(next.request_template, null, 2));
      toast("小七配置已保存", "success");
    } catch (err) {
      toast(err instanceof Error ? err.message : "保存失败", "error");
    } finally {
      setSaving(false);
    }
  }

  async function test(command: XiaoqiCommand) {
    setTesting(command);
    setResult(null);
    try {
      const r = await sendXiaoqiCommand({ command, question: command === "custom" ? customQ : undefined });
      if (r.sent) {
        toast(`已发送「${command}」命令`, "success");
        setResult(`HTTP ${r.status_code} · 响应：${typeof r.response === "string" ? r.response : JSON.stringify(r.response).slice(0, 600)}`);
      } else {
        toast(`未发送：${r.reason}`, "info");
        setResult(`未发送（${r.reason}）。将发送的请求体：\n${JSON.stringify(r.payload, null, 2)}`);
      }
    } catch (err) {
      toast(err instanceof Error ? err.message : "发送失败", "error");
    } finally {
      setTesting(null);
    }
  }

  async function pushRecord() {
    setPushing(true);
    setResult(null);
    try {
      const r = await pushXiaoqiMatchRecord(matchId);
      if (r.sent) {
        toast("已推送比赛记录到小七", "success");
        setResult(`HTTP ${r.status_code} · 响应：${typeof r.response === "string" ? r.response : JSON.stringify(r.response).slice(0, 600)}`);
      } else {
        toast(`未推送：${r.reason}`, "info");
        setResult(`未推送（${r.reason}）。将发送的请求体：\n${JSON.stringify(r.payload, null, 2)}`);
      }
    } catch (err) {
      toast(err instanceof Error ? err.message : "推送失败", "error");
    } finally {
      setPushing(false);
    }
  }

  return (
    <div className="space-y-5">
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="flex size-11 items-center justify-center rounded-lg bg-primary/10 text-primary">
                <Sparkles className="size-5" />
              </div>
              <div>
                <CardTitle>小七 · 自研智能体</CardTitle>
                <CardDescription>系统只负责发送命令，小七发音依赖其自身。请求体 / 地址在此设置。</CardDescription>
              </div>
            </div>
            <Switch checked={cfg.enabled} onCheckedChange={(v) => set("enabled", v)} />
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label>名称</Label>
              <Input value={cfg.name} onChange={(e) => set("name", e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label>请求方式</Label>
              <Select value={cfg.request_method} onChange={(e) => set("request_method", e.target.value)}>
                {["POST", "GET", "PUT", "PATCH"].map((m) => (
                  <option key={m}>{m}</option>
                ))}
              </Select>
            </div>
            <div className="space-y-1.5 sm:col-span-2">
              <Label>请求地址（小七命令接口）</Label>
              <Input value={cfg.endpoint} onChange={(e) => set("endpoint", e.target.value)} placeholder="https://…/xiaoqi" />
              <p className="text-xs text-muted-foreground">用于下发 自我介绍/点评/评判/自定义 命令。</p>
            </div>
            <div className="space-y-1.5 sm:col-span-2">
              <Label className="flex items-center gap-1.5"><Upload className="size-3.5" /> 比赛记录接口（match_record/update）</Label>
              <Input
                value={cfg.match_record_endpoint}
                onChange={(e) => set("match_record_endpoint", e.target.value)}
                placeholder="https://aitoys.seawayos.com/celebration-api/v1/match_record/update"
              />
              <p className="text-xs text-muted-foreground">把本场完整辩论记录（按阶段聚合）推送给小七，供其点评/评判/现场投票。</p>
            </div>
            <div className="space-y-1.5">
              <Label>会话 ID（session_id）</Label>
              <Input value={cfg.session_id} onChange={(e) => set("session_id", e.target.value)} placeholder="default" />
            </div>
            <div className="space-y-1.5">
              <Label>推送比赛记录</Label>
              <Button
                variant="outline"
                className="w-full justify-start"
                loading={pushing}
                disabled={!hasMatch}
                onClick={pushRecord}
              >
                <Upload /> 立即推送到小七
              </Button>
              {!hasMatch && <p className="text-xs text-muted-foreground">尚无比赛，先在「比赛管理」新建。</p>}
            </div>
            <div className="space-y-1.5">
              <Label>API Key 环境变量名</Label>
              <Input value={cfg.api_key_env} onChange={(e) => set("api_key_env", e.target.value)} placeholder="如：XIAOQI_API_KEY" />
              {cfg.api_key_configured && <Badge variant="success">环境变量已设置</Badge>}
            </div>
            <div className="space-y-1.5">
              <Label>超时（毫秒）</Label>
              <Input type="number" value={cfg.timeout_ms} onChange={(e) => set("timeout_ms", Number(e.target.value))} />
            </div>
          </div>

          <Separator />
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label className="flex items-center gap-1.5"><ImageIcon className="size-3.5" /> 形象图 URL</Label>
              <Input value={cfg.image_url} onChange={(e) => set("image_url", e.target.value)} placeholder="https://…/xiaoqi.png" />
              <p className="text-xs text-muted-foreground">用于小七观点页 / 结果页（替换二维码位置）。</p>
            </div>
            {cfg.image_url && (
              <div className="flex items-center justify-center rounded-md border border-border p-2">
                <img src={cfg.image_url} alt="小七形象" className="max-h-24 rounded object-contain" />
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">功能 Prompt 与测试</CardTitle>
          <CardDescription>编辑各功能 prompt，可直接点击测试，查看小七响应。</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {COMMANDS.map(({ key, label, icon: Icon }) => (
            <div key={key} className="space-y-1.5">
              <Label className="flex items-center gap-1.5">
                <Icon className="size-3.5" /> {label}
              </Label>
              {key === "custom" ? (
                <Input value={customQ} onChange={(e) => setCustomQ(e.target.value)} placeholder="输入要问小七的自定义问题" />
              ) : (
                <Textarea rows={2} value={cfg.prompts[key]} onChange={(e) => setPrompt(key, e.target.value)} className="font-sans" />
              )}
              <Button
                size="sm"
                variant="outline"
                loading={testing === key}
                disabled={key === "custom" && !customQ.trim()}
                onClick={() => test(key)}
              >
                <Send /> 测试发送
              </Button>
            </div>
          ))}
          {result && <Textarea readOnly rows={6} value={result} className="text-xs" />}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">请求体模板（JSON）</CardTitle>
          <CardDescription>支持占位符：{"{command}"} {"{prompt}"} {"{debate_topic}"} {"{debate_history}"}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          <Textarea rows={8} value={tplText} onChange={(e) => setTplText(e.target.value)} className="text-xs" />
          {tplError && <p className="text-xs text-destructive">{tplError}</p>}
        </CardContent>
      </Card>

      <div className="flex justify-end">
        <Button onClick={save} loading={saving}>
          <Save /> 保存全部配置
        </Button>
      </div>
    </div>
  );
}
