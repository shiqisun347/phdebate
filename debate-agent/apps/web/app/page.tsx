"use client";

import {
  Activity,
  Bot,
  BrainCircuit,
  FileText,
  Gauge,
  KeyRound,
  ListTree,
  LogOut,
  MessageSquareText,
  RefreshCw,
  Save,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

import { api, setCsrfToken } from "@/lib/api";

type Json = Record<string, any>;
type Dashboard = {
  counts: Json;
  providers: Json[];
  prompts: Json[];
  message_templates: Json[];
  presets: Json[];
  memory_policies: Json[];
  personas: Json[];
  memories: Json[];
  tasks: Json[];
  gateway_keys: Json[];
  admins: Json[];
  audit: Json[];
};

const tabs = [
  ["overview", "总览", Gauge],
  ["providers", "LLM API", Activity],
  ["prompts", "Prompt Studio", FileText],
  ["messages", "消息模板", MessageSquareText],
  ["personas", "辩手人设", Bot],
  ["parameters", "模型参数", SlidersHorizontal],
  ["memory", "Memory", BrainCircuit],
  ["requests", "请求日志", ListTree],
  ["security", "安全与管理员", ShieldCheck],
] as const;

const initialProvider = {
  name: "",
  provider: "openai",
  base_url: "",
  api_key: "",
  model_id: "",
  timeout_seconds: 120,
  priority: 100,
  max_retries: 2,
  rpm_limit: 60,
  is_active: true,
};

export default function AgentAdminPage() {
  const [session, setSession] = useState<Json | null>(null);
  const [login, setLogin] = useState({ account: "", password: "" });
  const [data, setData] = useState<Dashboard | null>(null);
  const [tab, setTab] = useState<(typeof tabs)[number][0]>("overview");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [provider, setProvider] = useState(initialProvider);
  const [prompt, setPrompt] = useState({ prompt_key: "", name: "", task_type: "debate", system_template: "", user_template: "" });
  const [message, setMessage] = useState({ key: "", name: "", task_type: "debate", stage_pattern: "*", role: "user", template: "", position: 100, is_active: true });
  const [preset, setPreset] = useState({ name: "", temperature: 0.7, top_p: 0.9, max_tokens: 700, presence_penalty: 0, frequency_penalty: 0, stop: "", is_active: true });
  const [policy, setPolicy] = useState({ name: "", mode: "layered", match_window_messages: 16, retrieval_count: 5, max_context_chars: 12000, retention_days: 180, long_term_requires_review: true, is_active: true });
  const [persona, setPersona] = useState({ profile_key: "", name: "", description: "", style_prompt: "", provider_id: "", prompt_version_id: "", model_preset_id: "", memory_policy_id: "", is_active: true });
  const [gatewayName, setGatewayName] = useState("主辩论平台");
  const [newGatewaySecret, setNewGatewaySecret] = useState("");
  const [adminForm, setAdminForm] = useState({ account: "", real_name: "", password: "", role: "admin" });
  const [memoryPreviewInput, setMemoryPreviewInput] = useState({ profile_key: "debater-1", match_id: "" });
  const [memoryPreview, setMemoryPreview] = useState<Json | null>(null);
  const [promptPreview, setPromptPreview] = useState<Json | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const [simpleProvider, setSimpleProvider] = useState<Json>({ id: "", ...initialProvider });
  const [providerSecret, setProviderSecret] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const current = await api<{ user: Json }>("/admin/session");
      setSession(current.user);
      setData(await api<Dashboard>("/admin/dashboard"));
    } catch (err) {
      setSession(null);
      setData(null);
      if (!(err instanceof Error) || !err.message.includes("会话")) setError(err instanceof Error ? err.message : "载入失败");
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!data?.providers.length || simpleProvider.id) return;
    const current = [...data.providers].sort((left, right) => Number(left.priority) - Number(right.priority))[0];
    setSimpleProvider({ ...current, api_key: "", clear_api_key: false });
  }, [data, simpleProvider.id]);

  async function submitLogin(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError("");
    try {
      const result = await api<{ user: Json; csrf_token: string }>("/admin/login", { method: "POST", body: JSON.stringify(login) });
      setCsrfToken(result.csrf_token);
      setSession(result.user);
      setLogin({ account: "", password: "" });
      setData(await api<Dashboard>("/admin/dashboard"));
    } catch (err) { setError(err instanceof Error ? err.message : "登录失败"); }
    finally { setBusy(false); }
  }

  async function mutate(path: string, body: unknown, success: string) {
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await api<Json>(path, { method: "POST", body: JSON.stringify(body) });
      if (result.secret) setNewGatewaySecret(result.secret);
      setNotice(success);
      setData(await api<Dashboard>("/admin/dashboard"));
      return result;
    } catch (err) { setError(err instanceof Error ? err.message : "保存失败"); }
    finally { setBusy(false); }
  }

  async function logout() {
    await api("/admin/logout", { method: "POST", body: "{}" });
    setSession(null); setData(null); setCsrfToken("");
  }

  async function previewMemory(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError("");
    try {
      const query = new URLSearchParams({ profile_key: memoryPreviewInput.profile_key });
      if (memoryPreviewInput.match_id) query.set("match_id", memoryPreviewInput.match_id);
      setMemoryPreview(await api<Json>(`/admin/memories/preview?${query}`));
    } catch (err) { setError(err instanceof Error ? err.message : "Memory 预览失败"); }
    finally { setBusy(false); }
  }

  async function previewCurrentPrompt() {
    setBusy(true); setError("");
    try {
      setPromptPreview(await api<Json>("/admin/prompts/preview", {
        method: "POST",
        body: JSON.stringify({ system_template: prompt.system_template, user_template: prompt.user_template, variables: {} }),
      }));
    } catch (err) { setError(err instanceof Error ? err.message : "Prompt 预览失败"); }
    finally { setBusy(false); }
  }

  async function saveSimpleProvider(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError(""); setNotice("");
    try {
      const path = simpleProvider.id ? `/admin/llm-providers/${simpleProvider.id}` : "/admin/llm-providers";
      const result = await api<{ provider: Json }>(path, {
        method: simpleProvider.id ? "PUT" : "POST",
        body: JSON.stringify({
          name: simpleProvider.name,
          provider: simpleProvider.provider,
          base_url: simpleProvider.base_url,
          api_key: providerSecret || null,
          clear_api_key: false,
          model_id: simpleProvider.model_id,
          timeout_seconds: Number(simpleProvider.timeout_seconds || 180),
          priority: Number(simpleProvider.priority || 1),
          max_retries: Number(simpleProvider.max_retries || 1),
          rpm_limit: Number(simpleProvider.rpm_limit || 60),
          is_active: Boolean(simpleProvider.is_active),
        }),
      });
      setSimpleProvider({ ...result.provider, api_key: "" });
      setProviderSecret("");
      setNotice("模型服务已保存。系统会自动使用推荐的辩论参数。");
      setData(await api<Dashboard>("/admin/dashboard"));
    } catch (err) { setError(err instanceof Error ? err.message : "模型服务保存失败"); }
    finally { setBusy(false); }
  }

  async function testSimpleProvider() {
    if (!simpleProvider.id) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await api<Json>(`/admin/llm-providers/${simpleProvider.id}/test`, { method: "POST", body: "{}" });
      setNotice(`连接成功 · ${result.latency_ms} ms · 共发现 ${result.models_count} 个模型${result.model_available ? " · 当前模型可用" : " · 当前模型未出现在列表中"}`);
    } catch (err) { setError(err instanceof Error ? err.message : "连接测试失败"); }
    finally { setBusy(false); }
  }

  const publishedPrompts = useMemo(() => data?.prompts.filter((item) => item.status === "published") || [], [data]);

  if (!session) return (
    <main className="login-shell">
      <section className="login-card">
        <div className="brand-mark"><BrainCircuit /><span>JIXIA</span></div>
        <span className="eyebrow">Independent Agent Control Plane</span>
        <h1>Debate Agent 管理平台</h1>
        <p>独立管理 LLM、Prompt、Memory、人设和 RESTful Gateway。</p>
        {error && <div className="error">{error}</div>}
        <form onSubmit={submitLogin} className="stack">
          <label>管理员账号<input required autoComplete="username" value={login.account} onChange={(event) => setLogin({ ...login, account: event.target.value })} /></label>
          <label>密码<input required type="password" autoComplete="current-password" value={login.password} onChange={(event) => setLogin({ ...login, password: event.target.value })} /></label>
          <button disabled={busy}>{busy ? "登录中…" : "登录控制台"}</button>
        </form>
      </section>
    </main>
  );
  if (!data) return <div className="loading">正在载入 Agent 控制平面…</div>;

  if (!advanced) return (
    <div style={{ minHeight: "100vh" }}>
      <header className="topbar" style={{ position: "sticky" }}>
        <div className="brand-mark"><BrainCircuit /><span>JIXIA AGENT</span></div>
        <div className="top-actions"><span>{session.real_name}</span><button className="ghost" onClick={() => setAdvanced(true)}><Settings2 />高级模式</button><button className="ghost" onClick={() => void logout()}><LogOut />退出</button></div>
      </header>
      <main className="content" style={{ margin: "0 auto" }}>
        <PageTitle title="辩论 Agent 简易设置" detail="只需配置地址、密钥和模型，其余参数由系统自动管理" />
        {error && <div className="error">{error}</div>}
        {notice && <div className="notice">{notice}</div>}
        <div className="stats">
          <article><span>模型服务</span><strong>{data.providers.filter((item) => item.is_active).length}</strong></article>
          <article><span>AI 辩手</span><strong>{data.personas.filter((item) => item.is_active).length}</strong></article>
          <article><span>运行任务</span><strong>{data.counts.running_tasks || 0}</strong></article>
          <article><span>待审核记忆</span><strong>{data.counts.pending_memories || 0}</strong></article>
        </div>
        <div className="grid">
          <form className="panel stack" onSubmit={saveSimpleProvider}>
            <div className="panel-head"><h3>模型服务</h3><span>三项必填</span></div>
            {data.providers.length > 1 && <label>选择配置<select value={simpleProvider.id} onChange={(event) => { const item = data.providers.find((provider) => provider.id === event.target.value); if (item) setSimpleProvider({ ...item, api_key: "" }); }}>{data.providers.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>}
            <label>接入方式<select value={simpleProvider.provider || "openai"} onChange={(event) => setSimpleProvider({ ...simpleProvider, provider: event.target.value })}><option value="openai">OpenAI 兼容 LLM API（推荐）</option><option value="debate_api">现有 Debate API（回退）</option></select></label>
            <Field label="API 地址" value={String(simpleProvider.base_url || "")} set={(base_url) => setSimpleProvider({ ...simpleProvider, base_url })} type="url" placeholder="http://服务器:端口/v1" />
            <Field label="模型名称" value={String(simpleProvider.model_id || "")} set={(model_id) => setSimpleProvider({ ...simpleProvider, model_id })} placeholder="模型 ID" />
            <label>API Key（已有密钥时可留空）<input type="password" autoComplete="new-password" value={providerSecret} onChange={(event) => setProviderSecret(event.target.value)} placeholder={simpleProvider.has_api_key ? "已配置，留空保持不变" : "输入 API Key"} /></label>
            <label><input type="checkbox" checked={Boolean(simpleProvider.is_active)} onChange={(event) => setSimpleProvider({ ...simpleProvider, is_active: event.target.checked })} />启用该模型服务</label>
            <div className="actions"><button disabled={busy}><Save />保存</button><button type="button" className="ghost" disabled={busy || !simpleProvider.id} onClick={() => void testSimpleProvider()}><Activity />测试连接</button></div>
          </form>
          <section className="panel stack">
            <div className="panel-head"><h3>API 调用方式</h3><span>与原服务兼容</span></div>
            <p>请求地址：<code>POST /debate/api/debate</code></p>
            <p><strong>运行模式：</strong>上游流式生成已开启，Thinking 已在服务端关闭。</p>
            <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", background: "#07101d", padding: 14, borderRadius: 10 }}>{`{
  "model_name": "兼容字段，正式配置由服务端决定",
  "debater_name": "陈思远",
  "debate_position": "一辩",
  "debate_topic": "人工智能时代，还要不要学编程？",
  "current_stage": "自我介绍",
  "next_stage": "正方一辩立论",
  "holder": "正方",
  "debate_history": [],
  "task_type": "self_intro",
  "max_token": 186
}`}</pre>
            <small>响应使用与原服务一致的 SSE：每条消息包含 delta.content，最后发送 [DONE]。</small>
          </section>
        </div>
        <section className="panel stack" style={{ marginTop: 18 }}>
          <div className="panel-head"><h3>AI 裁判</h3><span>已启用</span></div>
          <p>裁判接口：<code>POST /debate/api/judge</code></p>
          <p>裁判使用当前主 LLM 和独立的严格 JSON 裁判规则，主辩论系统会在总结结束后自动调用。</p>
        </section>
        <h2>AI 辩手</h2>
        <Table columns={["Profile Key", "姓名", "状态"]} rows={data.personas.map((item) => [item.profile_key, item.name, item.is_active ? "启用" : "停用"])} />
      </main>
    </div>
  );

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-mark"><BrainCircuit /><span>JIXIA AGENT</span></div>
        <div className="top-actions"><span>{session.real_name} · {session.role}</span><button className="ghost" onClick={() => setAdvanced(false)}>简易模式</button><button className="ghost" onClick={() => void load()}><RefreshCw />刷新</button><button className="ghost" onClick={() => void logout()}><LogOut />退出</button></div>
      </header>
      <aside className="sidebar">
        <p>CONTROL PLANE</p>
        {tabs.map(([id, label, Icon]) => <button key={id} className={tab === id ? "active" : ""} onClick={() => setTab(id)}><Icon />{label}</button>)}
      </aside>
      <main className="content">
        {error && <div className="error">{error}</div>}
        {notice && <div className="notice">{notice}</div>}
        {tab === "overview" && <><PageTitle title="Agent 服务总览" detail="配置、运行状态与待处理事项" /><div className="stats">{Object.entries(data.counts).map(([key, value]) => <article key={key}><span>{key.replaceAll("_", " ")}</span><strong>{String(value)}</strong></article>)}</div><div className="grid"><Panel title="Provider 状态">{data.providers.map((item) => <Row key={item.id} title={`${item.name} · ${item.model_id}`} detail={`${item.provider} · ${item.is_active ? "启用" : "停用"}`} />)}</Panel><Panel title="最近任务">{data.tasks.slice(0, 8).map((item) => <Row key={item.id} title={`${item.room_code || "无房间"} · ${item.profile_key}`} detail={`${item.status} · ${item.latency_ms || 0} ms`} />)}</Panel></div></>}
        {tab === "providers" && <><PageTitle title="LLM 与上游 API" detail="支持 LiteLLM Provider、现有 Debate REST API 与自动 Fallback" /><form className="panel stack" onSubmit={(event) => { event.preventDefault(); void mutate("/admin/llm-providers", provider, "Provider 已创建"); }}><div className="form-grid"><Field label="名称" value={provider.name} set={(name) => setProvider({ ...provider, name })} /><Field label="Provider 类型" value={provider.provider} set={(value) => setProvider({ ...provider, provider: value })} placeholder="debate_api / openai / deepseek" /><Field label="模型 ID" value={provider.model_id} set={(model_id) => setProvider({ ...provider, model_id })} /><Field label="Base URL / Debate Endpoint" value={provider.base_url} set={(base_url) => setProvider({ ...provider, base_url })} type="url" /><Field label="API Key（可选）" value={provider.api_key} set={(api_key) => setProvider({ ...provider, api_key })} type="password" /></div><div className="form-grid compact"><NumberField label="超时秒" value={provider.timeout_seconds} set={(timeout_seconds) => setProvider({ ...provider, timeout_seconds })} /><NumberField label="优先级" value={provider.priority} set={(priority) => setProvider({ ...provider, priority })} /><NumberField label="重试" value={provider.max_retries} set={(max_retries) => setProvider({ ...provider, max_retries })} /><NumberField label="RPM" value={provider.rpm_limit} set={(rpm_limit) => setProvider({ ...provider, rpm_limit })} /></div><button disabled={busy}><Save />创建 Provider</button></form><Table columns={["名称", "Provider / 模型", "地址", "状态"]} rows={data.providers.map((item) => [item.name, `${item.provider} / ${item.model_id}`, item.base_url || "默认", item.is_active ? `启用${item.has_api_key ? " · Key 已配置" : ""}` : "停用"])} /></>}
        {tab === "prompts" && <><PageTitle title="Prompt Studio" detail="新建不可变版本，审核后发布并绑定人设" /><form className="panel stack" onSubmit={(event) => { event.preventDefault(); void mutate("/admin/prompts", { ...prompt, variables_schema: {} }, "Prompt 草稿版本已创建"); }}><div className="form-grid"><Field label="Prompt Key" value={prompt.prompt_key} set={(prompt_key) => setPrompt({ ...prompt, prompt_key })} placeholder="debate-main" /><Field label="名称" value={prompt.name} set={(name) => setPrompt({ ...prompt, name })} /><Field label="任务类型" value={prompt.task_type} set={(task_type) => setPrompt({ ...prompt, task_type })} /></div><Area label="System Prompt" value={prompt.system_template} set={(system_template) => setPrompt({ ...prompt, system_template })} /><Area label="User Prompt" value={prompt.user_template} set={(user_template) => setPrompt({ ...prompt, user_template })} /><div className="actions"><button type="button" className="ghost" disabled={busy || !prompt.system_template || !prompt.user_template} onClick={() => void previewCurrentPrompt()}>预览渲染</button><button disabled={busy}><Save />创建草稿版本</button></div>{promptPreview && <div className="prompt-preview">{promptPreview.messages.map((item: Json, index: number) => <article key={index}><strong>{item.role}</strong><pre>{item.content}</pre></article>)}</div>}</form><div className="cards">{data.prompts.map((item) => <article className="panel" key={item.id}><div className="panel-head"><strong>{item.name} v{item.version}</strong><span>{item.status}</span></div><code>{item.prompt_key}</code><p>{item.task_type}</p>{item.status !== "published" && <button onClick={() => void mutate(`/admin/prompts/${item.id}/publish`, {}, "Prompt 已发布")}>发布</button>}</article>)}</div></>}
        {tab === "messages" && <><PageTitle title="消息模板" detail="按任务、环节和角色追加结构化消息" /><form className="panel stack" onSubmit={(event) => { event.preventDefault(); void mutate("/admin/message-templates", message, "消息模板已创建"); }}><div className="form-grid"><Field label="Key" value={message.key} set={(key) => setMessage({ ...message, key })} /><Field label="名称" value={message.name} set={(name) => setMessage({ ...message, name })} /><Field label="任务类型" value={message.task_type} set={(task_type) => setMessage({ ...message, task_type })} /><Field label="环节匹配" value={message.stage_pattern} set={(stage_pattern) => setMessage({ ...message, stage_pattern })} /></div><label>消息角色<select value={message.role} onChange={(e) => setMessage({ ...message, role: e.target.value })}><option>system</option><option>user</option><option>assistant</option></select></label><Area label="模板" value={message.template} set={(template) => setMessage({ ...message, template })} /><button disabled={busy}>创建消息模板</button></form><Table columns={["Key", "名称", "任务 / 环节", "角色"]} rows={data.message_templates.map((item) => [item.key, item.name, `${item.task_type} / ${item.stage_pattern}`, item.role])} /></>}
        {tab === "parameters" && <><PageTitle title="模型参数预设" detail="外部请求不能覆盖正式发布的参数" /><form className="panel stack" onSubmit={(event) => { event.preventDefault(); void mutate("/admin/model-presets", { ...preset, stop: preset.stop.split("\n").filter(Boolean), reasoning: {} }, "参数预设已创建"); }}><div className="form-grid"><Field label="名称" value={preset.name} set={(name) => setPreset({ ...preset, name })} /><NumberField label="Temperature" value={preset.temperature} set={(temperature) => setPreset({ ...preset, temperature })} step="0.05" /><NumberField label="Top P" value={preset.top_p} set={(top_p) => setPreset({ ...preset, top_p })} step="0.05" /><NumberField label="Max Tokens" value={preset.max_tokens} set={(max_tokens) => setPreset({ ...preset, max_tokens })} /></div><Area label="Stop（每行一项）" value={preset.stop} set={(stop) => setPreset({ ...preset, stop })} /><button disabled={busy}>创建参数预设</button></form><Table columns={["名称", "Temperature", "Top P", "Max Tokens"]} rows={data.presets.map((item) => [item.name, item.temperature, item.top_p, item.max_tokens])} /></>}
        {tab === "personas" && <><PageTitle title="辩手人设" detail="稳定 profile_key 与主辩论平台席位池绑定" /><form className="panel stack" onSubmit={(event) => { event.preventDefault(); void mutate("/admin/personas", persona, "辩手人设已创建"); }}><div className="form-grid"><Field label="Profile Key" value={persona.profile_key} set={(profile_key) => setPersona({ ...persona, profile_key })} /><Field label="姓名" value={persona.name} set={(name) => setPersona({ ...persona, name })} /><SelectField label="LLM Provider" value={persona.provider_id} set={(provider_id) => setPersona({ ...persona, provider_id })} items={data.providers} /><SelectField label="Prompt 版本" value={persona.prompt_version_id} set={(prompt_version_id) => setPersona({ ...persona, prompt_version_id })} items={publishedPrompts} labelKey="name" /></div><div className="form-grid"><SelectField label="参数预设" value={persona.model_preset_id} set={(model_preset_id) => setPersona({ ...persona, model_preset_id })} items={data.presets} /><SelectField label="Memory 策略" value={persona.memory_policy_id} set={(memory_policy_id) => setPersona({ ...persona, memory_policy_id })} items={data.memory_policies} /></div><Area label="风格与策略" value={persona.style_prompt} set={(style_prompt) => setPersona({ ...persona, style_prompt })} /><button disabled={busy}>创建人设</button></form><Table columns={["Profile Key", "姓名", "Provider", "状态"]} rows={data.personas.map((item) => [item.profile_key, item.name, data.providers.find((provider) => provider.id === item.provider_id)?.name || item.provider_id, item.is_active ? "启用" : "停用"])} /></>}
        {tab === "memory" && <><PageTitle title="Memory" detail="比赛内自动使用；跨比赛候选需管理员审核" /><form className="panel stack" onSubmit={(event) => { event.preventDefault(); void mutate("/admin/memory-policies", policy, "Memory 策略已创建"); }}><div className="form-grid"><Field label="策略名称" value={policy.name} set={(name) => setPolicy({ ...policy, name })} /><label>模式<select value={policy.mode} onChange={(event) => setPolicy({ ...policy, mode: event.target.value })}><option value="layered">分层审核</option><option value="match">仅单场</option><option value="disabled">停用</option></select></label><NumberField label="历史窗口" value={policy.match_window_messages} set={(match_window_messages) => setPolicy({ ...policy, match_window_messages })} /><NumberField label="检索条数" value={policy.retrieval_count} set={(retrieval_count) => setPolicy({ ...policy, retrieval_count })} /></div><button disabled={busy}>创建策略</button></form><form className="panel stack" onSubmit={previewMemory}><h3>检索预览</h3><div className="form-grid"><Field label="Profile Key" value={memoryPreviewInput.profile_key} set={(profile_key) => setMemoryPreviewInput({ ...memoryPreviewInput, profile_key })} /><label>Match ID（留空验证无状态）<input value={memoryPreviewInput.match_id} onChange={(event) => setMemoryPreviewInput({ ...memoryPreviewInput, match_id: event.target.value })} /></label></div><button disabled={busy}>预览本次可检索记忆</button>{memoryPreview && <div className="memory-list"><Row title={memoryPreview.stateless ? "无状态请求" : `比赛 ${memoryPreview.match_id}`} detail={`单场 ${memoryPreview.match_memory.length} 条 · 已审核长期 ${memoryPreview.approved_long_term_memory.length} 条`} />{[...memoryPreview.match_memory, ...memoryPreview.approved_long_term_memory].map((item: Json) => <Row key={item.id} title={`${item.scope} · ${item.status}`} detail={item.content} />)}</div>}</form><div className="memory-list">{data.memories.map((item) => <article className="panel" key={item.id}><div className="panel-head"><strong>{item.profile_key} · {item.scope}</strong><span>{item.status}</span></div><p>{item.content}</p><small>{item.match_id || "跨比赛"} · {item.created_at}</small>{item.status === "pending_review" && <div className="actions"><button onClick={() => void mutate(`/admin/memories/${item.id}/approve`, {}, "长期记忆已批准")}>批准</button><button className="danger" onClick={() => void mutate(`/admin/memories/${item.id}/reject`, {}, "候选记忆已拒绝")}>拒绝</button></div>}</article>)}</div></>}
        {tab === "requests" && <><PageTitle title="请求与审计日志" detail="按任务记录配置版本、延迟、Token、错误和中断" /><Table columns={["Task", "房间 / 比赛", "人设", "状态", "延迟"]} rows={data.tasks.map((item) => [item.task_id, `${item.room_code || "-"} / ${item.match_id || "-"}`, item.profile_key, item.status, `${item.latency_ms || 0} ms`])} /><h2>管理员审计</h2><Table columns={["时间", "动作", "对象", "操作者"]} rows={data.audit.map((item) => [item.created_at, item.action, `${item.target_type}:${item.target_id}`, item.actor_user_id || "system"])} /></>}
        {tab === "security" && <><PageTitle title="安全与管理员" detail="Gateway Key 仅创建时显示一次；服务端只保存哈希" /><div className="grid"><form className="panel stack" onSubmit={(event) => { event.preventDefault(); void mutate("/admin/gateway-keys", { name: gatewayName }, "Gateway Key 已创建，请立即复制"); }}><h3><KeyRound />Gateway Key</h3><Field label="名称" value={gatewayName} set={setGatewayName} />{newGatewaySecret && <div className="secret-once"><strong>仅显示一次</strong><code>{newGatewaySecret}</code></div>}<button disabled={busy}>创建 Gateway Key</button>{data.gateway_keys.map((item) => <Row key={item.id} title={`${item.name} · ${item.key_prefix}`} detail={item.is_active ? "启用" : "已撤销"} />)}</form><form className="panel stack" onSubmit={(event) => { event.preventDefault(); void mutate("/admin/admin-users", adminForm, "管理员已创建"); }}><h3><ShieldCheck />管理员</h3><Field label="账号" value={adminForm.account} set={(account) => setAdminForm({ ...adminForm, account })} /><Field label="真实姓名" value={adminForm.real_name} set={(real_name) => setAdminForm({ ...adminForm, real_name })} /><Field label="初始密码" value={adminForm.password} set={(password) => setAdminForm({ ...adminForm, password })} type="password" /><button disabled={busy || session.role !== "owner"}>创建管理员</button>{data.admins.map((item) => <Row key={item.id} title={`${item.real_name} · ${item.account}`} detail={item.role} />)}</form></div></>}
      </main>
    </div>
  );
}

function PageTitle({ title, detail }: { title: string; detail: string }) { return <div className="page-title"><div><span className="eyebrow">DEBATE AGENT</span><h1>{title}</h1><p>{detail}</p></div><Settings2 /></div>; }
function Panel({ title, children }: { title: string; children: React.ReactNode }) { return <section className="panel"><div className="panel-head"><h3>{title}</h3></div>{children}</section>; }
function Row({ title, detail }: { title: string; detail: string }) { return <div className="row"><strong>{title}</strong><small>{detail}</small></div>; }
function Field({ label, value, set, type = "text", placeholder = "" }: { label: string; value: string; set: (value: string) => void; type?: string; placeholder?: string }) { return <label>{label}<input required type={type} value={value} placeholder={placeholder} onChange={(event) => set(event.target.value)} /></label>; }
function NumberField({ label, value, set, step = "1" }: { label: string; value: number; set: (value: number) => void; step?: string }) { return <label>{label}<input required type="number" step={step} value={value} onChange={(event) => set(Number(event.target.value))} /></label>; }
function Area({ label, value, set }: { label: string; value: string; set: (value: string) => void }) { return <label>{label}<textarea required value={value} onChange={(event) => set(event.target.value)} /></label>; }
function SelectField({ label, value, set, items, labelKey = "name" }: { label: string; value: string; set: (value: string) => void; items: Json[]; labelKey?: string }) { return <label>{label}<select required value={value} onChange={(event) => set(event.target.value)}><option value="">请选择</option>{items.map((item) => <option key={item.id} value={item.id}>{item[labelKey]}{item.version ? ` v${item.version}` : ""}</option>)}</select></label>; }
function Table({ columns, rows }: { columns: string[]; rows: any[][] }) { return <div className="table-wrap"><table><thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={index}>{row.map((cell, cellIndex) => <td key={cellIndex}>{String(cell)}</td>)}</tr>)}</tbody></table>{!rows.length && <div className="empty">暂无数据</div>}</div>; }
