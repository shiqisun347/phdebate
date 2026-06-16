import * as React from "react";
import { ShieldCheck, ShieldOff, Lock, Eye, EyeOff } from "lucide-react";
import { Button, Card, CardContent, CardHeader, CardTitle, CardDescription, Input, Label, Switch, Badge, Spinner } from "../ui/primitives";
import { useToast } from "../lib/toast";
import { getRuntimeAuthStatus, updateRuntimeAuthStatus } from "../../api/client";
import type { RuntimeAuthStatus } from "../../types/contracts";

export function Security() {
  const toast = useToast();
  const [status, setStatus] = React.useState<RuntimeAuthStatus | null>(null);
  const [authRequired, setAuthRequired] = React.useState(false);
  const [password, setPassword] = React.useState("");
  const [show, setShow] = React.useState(false);
  const [saving, setSaving] = React.useState(false);

  const load = React.useCallback(async () => {
    try {
      const s = await getRuntimeAuthStatus();
      setStatus(s);
      setAuthRequired(s.auth_required);
    } catch (err) {
      toast(err instanceof Error ? err.message : "加载失败", "error");
    }
  }, [toast]);

  React.useEffect(() => {
    void load();
  }, [load]);

  async function save() {
    if (authRequired && !status?.runtime_configured && !password.trim()) {
      toast("开启安全登录需要先设置密码", "error");
      return;
    }
    setSaving(true);
    try {
      const body: Parameters<typeof updateRuntimeAuthStatus>[0] = {
        auth_required: authRequired,
        reason: "admin_security_update",
      };
      if (password.trim()) {
        const pwd = password.trim();
        // one shared password for admin / host / screen / debater pages
        body.tokens = { admin: pwd, host: pwd, screen: pwd, speaker_shared: pwd };
      }
      const next = await updateRuntimeAuthStatus(body);
      setStatus(next);
      setAuthRequired(next.auth_required);
      setPassword("");
      toast("安全设置已更新", "success");
    } catch (err) {
      toast(err instanceof Error ? err.message : "保存失败", "error");
    } finally {
      setSaving(false);
    }
  }

  if (!status) {
    return (
      <div className="flex items-center gap-2 py-20 text-muted-foreground">
        <Spinner /> 正在加载安全设置…
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-2xl space-y-5">
      <Card>
        <CardHeader>
          <div className="flex items-center gap-3">
            <div className={`flex size-11 items-center justify-center rounded-lg ${status.auth_required ? "bg-success/12 text-success" : "bg-muted text-muted-foreground"}`}>
              {status.auth_required ? <ShieldCheck className="size-5" /> : <ShieldOff className="size-5" />}
            </div>
            <div>
              <CardTitle>安全登录</CardTitle>
              <CardDescription>开启后，进入后台管理与辩手页面均需输入密码。该密码对所有比赛通用。</CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="flex items-center justify-between rounded-lg border border-border p-4">
            <div>
              <p className="text-sm font-medium text-foreground">启用安全登录</p>
              <p className="text-xs text-muted-foreground">
                当前状态：
                <Badge variant={status.auth_required ? "success" : "muted"} className="ml-1">
                  {status.auth_required ? "已开启" : "已关闭"}
                </Badge>
                {status.runtime_configured && <Badge variant="secondary" className="ml-1">密码已设置</Badge>}
              </p>
            </div>
            <Switch checked={authRequired} onCheckedChange={setAuthRequired} />
          </div>

          <div className="space-y-1.5">
            <Label>{status.runtime_configured ? "重置密码（留空则不修改）" : "设置密码"}</Label>
            <div className="relative">
              <Lock className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                type={show ? "text" : "password"}
                className="px-9"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="输入统一访问密码"
              />
              <button
                type="button"
                onClick={() => setShow((v) => !v)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
              >
                {show ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
              </button>
            </div>
            <p className="text-xs text-muted-foreground">密码以哈希方式存储于服务器，前端与接口都不会保存明文。</p>
          </div>

          <div className="flex justify-end">
            <Button loading={saving} onClick={save}>
              保存设置
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">凭据来源</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
            {status.roles.map((role) => {
              const src = status.token_sources[role];
              const configured = !!(src?.runtime_count || src?.file_count || src?.env_count || src?.env);
              return (
                <div key={role} className="rounded-md border border-border p-2.5 text-center">
                  <p className="text-xs text-muted-foreground">{role}</p>
                  <Badge variant={configured ? "success" : "muted"} className="mt-1">
                    {configured ? "已配置" : "未配置"}
                  </Badge>
                </div>
              );
            })}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
