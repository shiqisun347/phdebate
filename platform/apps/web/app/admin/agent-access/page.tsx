"use client";

import Link from "next/link";
import { Activity, ArrowLeft, ExternalLink, KeyRound, PlugZap, Save } from "lucide-react";
import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useState } from "react";

import { LoadError } from "@/components/load-error";
import { apiFetch } from "@/lib/api";
import { useSession } from "@/lib/use-session";

type AgentConfig = {
  id: string;
  kind: "agent";
  endpoint: string;
  settings: {
    method?: "POST";
    protocol?: "restful";
    health_endpoint?: string;
    timeout_seconds?: number;
    stream?: boolean;
  };
  has_secret: boolean;
  is_active: boolean;
  updated_at: string;
};

function emptyConfig(origin: string): AgentConfig {
  return {
    id: "",
    kind: "agent",
    endpoint: `${origin}/debate/api/debate`,
    settings: {
      method: "POST",
      protocol: "restful",
      health_endpoint: `${origin}/debate/api/health`,
      timeout_seconds: 120,
      stream: true,
    },
    has_secret: false,
    is_active: true,
    updated_at: "",
  };
}

export default function AgentAccessPage() {
  const router = useRouter();
  const { user, loading } = useSession();
  const [config, setConfig] = useState<AgentConfig | null>(null);
  const [secret, setSecret] = useState("");
  const [clearSecret, setClearSecret] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState<"save" | "test" | "">("");

  useEffect(() => {
    if (!loading && user?.role !== "system_admin") router.replace("/");
  }, [user, loading, router]);

  async function load() {
    setError("");
    try {
      const result = await apiFetch<{ items: AgentConfig[] }>("/api/admin/providers");
      setConfig(result.items.find((item) => item.kind === "agent") || emptyConfig(window.location.origin));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Agent 接入配置载入失败");
    }
  }

  useEffect(() => {
    if (user?.role === "system_admin") void load();
  }, [user?.role]);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!config) return;
    setBusy("save");
    setError("");
    setNotice("");
    try {
      const result = await apiFetch<{ provider: AgentConfig }>("/api/admin/providers/agent", {
        method: "PUT",
        body: JSON.stringify({
          endpoint: config.endpoint,
          settings: {
            health_endpoint: config.settings.health_endpoint,
            timeout_seconds: config.settings.timeout_seconds,
            stream: config.settings.stream,
          },
          secret: secret || null,
          clear_secret: clearSecret,
          is_active: config.is_active,
        }),
      });
      setConfig(result.provider);
      setSecret("");
      setClearSecret(false);
      setNotice("RESTful Agent 接入已保存，只影响之后开始的比赛。");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Agent 接入保存失败");
    } finally {
      setBusy("");
    }
  }

  async function testConnection() {
    setBusy("test");
    setError("");
    setNotice("");
    try {
      const result = await apiFetch<{ latency_ms: number; health: { status?: string; model?: string } }>(
        "/api/admin/providers/agent/test",
        { method: "POST", body: "{}" },
      );
      setNotice(`连接成功 · ${result.latency_ms} ms · ${result.health.status || "ready"}${result.health.model ? ` · ${result.health.model}` : ""}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Agent 连接测试失败");
    } finally {
      setBusy("");
    }
  }

  if (!loading && user?.role !== "system_admin") return <div className="loading-screen">正在验证管理员权限…</div>;
  if (!config && error) return <LoadError message={error} retry={() => void load()} />;
  if (!config) return <div className="loading-screen">正在载入 Agent 接入配置…</div>;

  return (
    <div className="page-shell">
      <div className="section-head">
        <div>
          <span className="eyebrow">System Integration</span>
          <h2>RESTful Agent 接入</h2>
          <p>主辩论平台只负责调用统一网关；LLM、Prompt、Memory 与模型参数在独立 Agent 控制台管理。</p>
        </div>
        <div className="hero-actions">
          <Link className="button button-secondary" href="/admin"><ArrowLeft size={17} />返回系统后台</Link>
          <a className="button" href="/debate" target="_blank" rel="noreferrer"><ExternalLink size={17} />打开 Agent 控制台</a>
        </div>
      </div>

      {error && <div className="error-box" role="alert">{error}</div>}
      {notice && <div className="notice-box" role="status">{notice}</div>}

      <div className="dashboard-grid">
        <form className="panel form-stack" onSubmit={save}>
          <div className="panel-title"><h2><PlugZap size={19} />网关配置</h2><span className="badge">仅 RESTful</span></div>
          <div className="form-grid">
            <div className="field"><label>接入协议</label><input className="input" value="RESTful HTTP" disabled /></div>
            <div className="field"><label>请求方法</label><input className="input" value="POST" disabled /></div>
          </div>
          <div className="field">
            <label htmlFor="agent-endpoint">发言接口</label>
            <input id="agent-endpoint" className="input" type="url" required maxLength={500} value={config.endpoint} onChange={(event) => setConfig({ ...config, endpoint: event.target.value })} />
          </div>
          <div className="field">
            <label htmlFor="agent-health-endpoint">健康检查接口</label>
            <input id="agent-health-endpoint" className="input" type="url" required maxLength={500} value={config.settings.health_endpoint || ""} onChange={(event) => setConfig({ ...config, settings: { ...config.settings, health_endpoint: event.target.value } })} />
          </div>
          <div className="form-grid">
            <div className="field">
              <label htmlFor="agent-timeout">响应超时（秒）</label>
              <input id="agent-timeout" className="input" type="number" min={10} max={300} value={config.settings.timeout_seconds || 120} onChange={(event) => setConfig({ ...config, settings: { ...config.settings, timeout_seconds: Number(event.target.value) } })} />
            </div>
            <label className="check-row" htmlFor="agent-stream"><input id="agent-stream" type="checkbox" checked={config.settings.stream !== false} onChange={(event) => setConfig({ ...config, settings: { ...config.settings, stream: event.target.checked } })} />启用 SSE 流式响应</label>
          </div>
          <div className="field">
            <label htmlFor="agent-secret"><KeyRound size={15} />共享密钥</label>
            <input id="agent-secret" className="input" type="password" autoComplete="new-password" maxLength={500} value={secret} onChange={(event) => setSecret(event.target.value)} placeholder={config.has_secret ? "已配置；留空保持不变" : "输入 X-Debate-Agent-Key"} disabled={clearSecret} />
            <small className="muted">密钥加密保存，不会返回浏览器，也不会写入比赛归档。</small>
          </div>
          {config.has_secret && <label className="check-row" htmlFor="agent-clear-secret"><input id="agent-clear-secret" type="checkbox" checked={clearSecret} onChange={(event) => setClearSecret(event.target.checked)} />清除现有共享密钥</label>}
          <label className="check-row" htmlFor="agent-active"><input id="agent-active" type="checkbox" checked={config.is_active} onChange={(event) => setConfig({ ...config, is_active: event.target.checked })} />新比赛启用该 Agent 网关</label>
          <div className="card-actions">
            <button className="button" disabled={Boolean(busy)}><Save size={17} />{busy === "save" ? "保存中…" : "保存接入配置"}</button>
            <button type="button" className="button button-secondary" disabled={Boolean(busy) || !config.is_active} onClick={() => void testConnection()}><Activity size={17} />{busy === "test" ? "检测中…" : "测试连接"}</button>
          </div>
        </form>

        <aside className="panel">
          <div className="panel-title"><h2>职责边界</h2></div>
          <div className="lobby-rule"><span>辩论平台</span><strong>比赛状态、轮次、上下文与超时</strong></div>
          <div className="lobby-rule"><span>Agent 服务</span><strong>LLM、Prompt、Memory、模板与采样参数</strong></div>
          <div className="lobby-rule"><span>鉴权请求头</span><strong><code>X-Debate-Agent-Key</code></strong></div>
          <div className="lobby-rule"><span>配置生效</span><strong>开赛时加密快照</strong></div>
          <p className="muted">辩手席位只保留姓名和语音音色标识；普通玩家无法查看或修改 Agent、模型及密钥配置。</p>
        </aside>
      </div>
    </div>
  );
}
