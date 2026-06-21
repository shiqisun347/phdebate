import * as React from "react";
import { Sparkles, Save, Send, ImageIcon, Upload, Link2, Braces } from "lucide-react";
import { Button, Card, CardContent, CardHeader, CardTitle, CardDescription, Input, Label, Textarea, Switch, Select, Badge, Spinner, Separator } from "../ui/primitives";
import { useToast } from "../lib/toast";
import { useAdminData } from "../lib/data";
import { getXiaoqi, updateXiaoqi, pushXiaoqiMatchRecord } from "../../api/client";
import type { XiaoqiConfig } from "../../types/contracts";

const DEFAULT_PUSH_ENDPOINT = "https://aitoys.seawayos.com/celebration-api/v1/match_record/update";

/** 比赛记录请求体示例（格式固定：{session_id, match_record:[{stage, message:[{speaker, content}]}]}）。 */
function matchRecordExample(sessionId: string): string {
  return JSON.stringify(
    {
      session_id: sessionId || "default",
      match_record: [
        {
          stage: "正方一辩立论",
          message: [{ speaker: "正方一辩", content: "谢谢主席，各位评委、对方辩友……" }],
        },
        {
          stage: "自由辩论",
          message: [
            { speaker: "正方二辩", content: "请问对方辩友……" },
            { speaker: "反方二辩", content: "对方辩友的问题我方认为……" },
          ],
        },
      ],
    },
    null,
    2
  );
}

export function Xiaoqi() {
  const toast = useToast();
  const { matchId, snapshot } = useAdminData();
  const [cfg, setCfg] = React.useState<XiaoqiConfig | null>(null);
  const [saving, setSaving] = React.useState(false);
  const [testing, setTesting] = React.useState(false);
  const [result, setResult] = React.useState<string | null>(null);
  const hasMatch = Boolean(snapshot?.match.id);

  const load = React.useCallback(async () => {
    try {
      const c = await getXiaoqi();
      // 预填充：接口地址留空时，默认填入 celebration-api 推送接口，便于直接保存使用。
      setCfg({ ...c, match_record_endpoint: c.match_record_endpoint || DEFAULT_PUSH_ENDPOINT });
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

  async function save() {
    setSaving(true);
    try {
      const next = await updateXiaoqi(cfg!);
      setCfg({ ...next, match_record_endpoint: next.match_record_endpoint || DEFAULT_PUSH_ENDPOINT });
      toast("小七配置已保存", "success");
    } catch (err) {
      toast(err instanceof Error ? err.message : "保存失败", "error");
    } finally {
      setSaving(false);
    }
  }

  async function runTest() {
    setTesting(true);
    setResult(null);
    try {
      const r = await pushXiaoqiMatchRecord(matchId);
      if (r.sent) {
        toast("给小七推送记录成功", "success");
        setResult(`HTTP ${r.status_code} · 响应：${typeof r.response === "string" ? r.response : JSON.stringify(r.response)}\n\n实际请求体：\n${JSON.stringify(r.payload, null, 2)}`);
      } else {
        toast(`未发送：${r.reason}`, "info");
        setResult(`未发送（${r.reason}）。将发送的请求体：\n${JSON.stringify(r.payload, null, 2)}`);
      }
    } catch (err) {
      toast(err instanceof Error ? err.message : "发送失败", "error");
    } finally {
      setTesting(false);
    }
  }

  return (
    <div className="space-y-5">
      {/* —— 小七接口信息 —— */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="flex size-11 items-center justify-center rounded-lg bg-primary/10 text-primary">
                <Sparkles className="size-5" />
              </div>
              <div>
                <CardTitle className="flex items-center gap-1.5"><Link2 className="size-4" /> 小七接口信息</CardTitle>
                <CardDescription>系统把当前辩论的比赛记录推送给小七，点评 / 评判 / 结果显示均由小七自身完成。</CardDescription>
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
              <Label className="flex items-center gap-1.5"><Upload className="size-3.5" /> 给小七推送接口</Label>
              <Input
                value={cfg.match_record_endpoint}
                onChange={(e) => set("match_record_endpoint", e.target.value)}
                placeholder={DEFAULT_PUSH_ENDPOINT}
              />
              <p className="text-xs text-muted-foreground">比赛记录无需单独接口，取当前辩论实况自动组装请求体后推送到此地址。</p>
            </div>
            <div className="space-y-1.5">
              <Label>会话 ID（session_id）</Label>
              <Input value={cfg.session_id} onChange={(e) => set("session_id", e.target.value)} placeholder="default" />
            </div>
            <div className="space-y-1.5">
              <Label>超时（毫秒）</Label>
              <Input type="number" value={cfg.timeout_ms} onChange={(e) => set("timeout_ms", Number(e.target.value))} />
            </div>
            <div className="space-y-1.5">
              <Label>API Key 环境变量名</Label>
              <Input value={cfg.api_key_env} onChange={(e) => set("api_key_env", e.target.value)} placeholder="如：XIAOQI_API_KEY" />
              {cfg.api_key_configured && <Badge variant="success">环境变量已设置</Badge>}
            </div>
            <div className="space-y-1.5">
              <Label className="flex items-center gap-1.5"><ImageIcon className="size-3.5" /> 形象图 URL</Label>
              <Input value={cfg.image_url} onChange={(e) => set("image_url", e.target.value)} placeholder="https://…/xiaoqi.png" />
              <p className="text-xs text-muted-foreground">用于大屏小七点评 / 评判页的形象展示。</p>
            </div>
          </div>
          {cfg.image_url && (
            <div className="flex items-center justify-center rounded-md border border-border p-2">
              <img src={cfg.image_url} alt="小七形象" className="max-h-24 rounded object-contain" />
            </div>
          )}
        </CardContent>
      </Card>

      {/* —— 请求体设置 —— */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-1.5 text-sm"><Braces className="size-4" /> 请求体设置</CardTitle>
          <CardDescription>请求体格式固定，由系统按当前辩论实况自动生成。</CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          <Label>比赛记录请求体（固定格式 · 预览）</Label>
          <Textarea readOnly rows={12} value={matchRecordExample(cfg.session_id)} className="text-xs font-mono" />
          <p className="text-xs text-muted-foreground">
            <code>match_record</code> 为比赛过程的历史记录列表，每个环节含 <code>stage</code> 与 <code>message</code>（<code>speaker</code> + <code>content</code>），由系统按本场实况自动生成。
          </p>
        </CardContent>
      </Card>

      {/* —— 请求测试 —— */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-1.5 text-sm"><Send className="size-4" /> 请求测试</CardTitle>
          <CardDescription>用当前比赛实况直接给小七推送一次记录，并查看请求体与响应。</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <Button variant="outline" loading={testing} disabled={!hasMatch} onClick={runTest}>
            <Upload /> 测试给小七推送记录
          </Button>
          {!hasMatch && <p className="text-xs text-muted-foreground">尚无比赛，先在「比赛管理」新建。</p>}
          {result && <Textarea readOnly rows={10} value={result} className="text-xs font-mono" />}
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
