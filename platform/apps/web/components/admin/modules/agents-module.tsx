import Link from "next/link";
import { Activity, Scale, Volume2 } from "lucide-react";
import type { Dispatch, FormEvent, SetStateAction } from "react";

import { providerStatusLabel } from "@/lib/status-labels";
import { healthDetail, providerDisplayLabels, type AdminDashboard } from "@/components/admin/system-overview";
import type { AgentProfileSummary, AudioCue, JudgeForm, JudgeProfile, ProviderConfig } from "@/components/admin/admin-module-types";

type Props = {
  dashboard: AdminDashboard;
  agents: AgentProfileSummary[];
  audioCues: AudioCue[];
  judges: JudgeProfile[];
  providerConfigs: ProviderConfig[];
  providerDrafts: Record<string, ProviderConfig>;
  judgeTarget: JudgeProfile | null;
  judgeForm: JudgeForm;
  saving: boolean;
  onProviderDraftsChange: Dispatch<SetStateAction<Record<string, ProviderConfig>>>;
  onJudgeFormChange: (value: JudgeForm) => void;
  onSaveProvider: (kind: ProviderConfig["kind"]) => void;
  onEditJudge: (judge?: JudgeProfile) => void;
  onSubmitJudge: (event: FormEvent) => void;
  onActivateJudge: (judge: JudgeProfile) => void;
  onPatchAgent: (id: string, patch: Record<string, unknown>) => void;
  onCreateAudioCue: (event: FormEvent<HTMLFormElement>) => void;
  onPatchAudioCue: (id: string, patch: Record<string, unknown>) => void;
};

export default function AgentsModule({
  dashboard,
  agents,
  audioCues,
  judges,
  providerConfigs,
  providerDrafts,
  judgeTarget,
  judgeForm,
  saving,
  onProviderDraftsChange,
  onJudgeFormChange,
  onSaveProvider,
  onEditJudge,
  onSubmitJudge,
  onActivateJudge,
  onPatchAgent,
  onCreateAudioCue,
  onPatchAudioCue,
}: Props) {
  const visibleProviders = Object.entries(dashboard.providers).filter(([key, value]) => key !== "lighttts" || value.enabled);
  const realtimeTtsHealth = dashboard.system_health.checks.moss_tts_realtime;

  return (
    <>
      <div className="panel-title">
        <h2>AI 辩手与语音服务</h2>
        <div className="card-actions"><Link className="button button-small" href="/admin/agent-access">RESTful Agent 接入</Link><span className="badge">玩家不可见</span></div>
      </div>
      <div className="service-grid">
        {visibleProviders.map(([key, value]) => (
          <div className="service-card" key={key}>
            <i className={`status-dot ${value.healthy === true ? "online" : value.enabled ? "warning" : ""}`} />
            <div><strong>{providerDisplayLabels[key] || key} · {providerStatusLabel[value.status] || value.status}</strong><small>{value.endpoint || "未配置"}{value.message ? ` · ${value.message}` : ""}</small></div>
          </div>
        ))}
      </div>
      <div className="panel-title" style={{ marginTop: 30 }}><h3><Activity size={18} />语音服务运行配置</h3><span className="badge">开赛时固定</span></div>
      <p className="muted">修改只影响之后开始的比赛；进行中的房间继续使用自己的语音配置快照。当前 MOSS 实时语音链路由服务器统一托管，此处只读，避免误改可靠播放基线。</p>
      <div className="provider-config-grid">
        {realtimeTtsHealth && (
          <section className="panel-subsection form-stack" aria-label="MOSS 实时语音合成运行状态">
            <div className="panel-title"><h3>MOSS 实时语音合成</h3><span className={`badge ${realtimeTtsHealth.ok ? "live" : "neg-badge"}`}>{realtimeTtsHealth.ok ? "当前运行" : "需要检查"}</span></div>
            <div className="service-card"><i className={`status-dot ${realtimeTtsHealth.ok ? "online" : "warning"}`} /><div><strong>{realtimeTtsHealth.ok ? "实时语音链路正常" : "实时语音链路异常"}</strong><small>{healthDetail(realtimeTtsHealth)}</small></div></div>
            <p className="muted">模型、并发、流式会话和浏览器播放参数由服务器部署配置管理；本页面不会修改这些参数。</p>
          </section>
        )}
        {providerConfigs.filter((item) => item.kind !== "agent" && (item.kind !== "lighttts" || item.is_active)).map((item) => {
          const draft = providerDrafts[item.kind] || item;
          return (
            <form className="panel-subsection form-stack" key={item.kind} onSubmit={(event) => { event.preventDefault(); onSaveProvider(item.kind); }}>
              <div className="panel-title"><h3>{item.kind === "funasr" ? "FunASR 语音识别" : "兼容 LightTTS 语音合成"}</h3><span className={`badge ${draft.is_active ? "" : "neg-badge"}`}>{draft.is_active ? "启用" : "停用"}</span></div>
              <div className="field"><label htmlFor={`provider-${item.kind}-endpoint`}>服务地址</label><input id={`provider-${item.kind}-endpoint`} className="input" required value={draft.endpoint} onChange={(event) => onProviderDraftsChange((current) => ({ ...current, [item.kind]: { ...draft, endpoint: event.target.value } }))} /></div>
              {item.kind === "funasr" ? (
                <div className="field"><label htmlFor="provider-funasr-final-wait">离线最终结果等待秒数</label><input id="provider-funasr-final-wait" className="input" type="number" min={5} max={60} step={0.5} value={draft.settings.final_wait_seconds ?? 30} onChange={(event) => onProviderDraftsChange((current) => ({ ...current, [item.kind]: { ...draft, settings: { ...draft.settings, final_wait_seconds: Number(event.target.value) } } }))} /></div>
              ) : (
                <div className="form-grid">
                  <div className="field"><label htmlFor="provider-lighttts-timeout">读取超时秒数</label><input id="provider-lighttts-timeout" className="input" type="number" min={30} max={300} value={draft.settings.read_timeout_seconds ?? 180} onChange={(event) => onProviderDraftsChange((current) => ({ ...current, [item.kind]: { ...draft, settings: { ...draft.settings, read_timeout_seconds: Number(event.target.value) } } }))} /></div>
                  <div className="field"><label htmlFor="provider-lighttts-speed">语速</label><input id="provider-lighttts-speed" className="input" type="number" min={0.5} max={2} step={0.05} value={draft.settings.speed ?? 1} onChange={(event) => onProviderDraftsChange((current) => ({ ...current, [item.kind]: { ...draft, settings: { ...draft.settings, speed: Number(event.target.value) } } }))} /></div>
                </div>
              )}
              <label className="check-row" htmlFor={`provider-${item.kind}-active`}><input id={`provider-${item.kind}-active`} type="checkbox" checked={draft.is_active} onChange={(event) => onProviderDraftsChange((current) => ({ ...current, [item.kind]: { ...draft, is_active: event.target.checked } }))} />新比赛启用该服务</label>
              <div className="card-actions"><button className="button button-small" disabled={saving}>保存 {item.kind}</button></div>
            </form>
          );
        })}
      </div>
      <div className="panel-title" style={{ marginTop: 30 }}><h3><Scale size={18} />AI 裁判配置</h3><button className="button button-small button-secondary" disabled={saving} onClick={() => onEditJudge()}>新建裁判</button></div>
      <p className="muted">开赛时会把当前启用配置固定到比赛快照；之后切换裁判不会改变进行中或历史比赛。非法胜方、NaN、越界分数和空判定理由会自动进入人工复核。</p>
      <form className="form-stack panel-subsection" onSubmit={onSubmitJudge}>
        <div className="form-grid">
          <div className="field"><label htmlFor="judge-name">配置名称</label><input id="judge-name" className="input" required maxLength={100} value={judgeForm.name} onChange={(event) => onJudgeFormChange({ ...judgeForm, name: event.target.value })} placeholder="正式赛 AI 裁判" /></div>
          <div className="field"><label htmlFor="judge-model">模型名称</label><input id="judge-model" className="input" required maxLength={120} value={judgeForm.model_name} onChange={(event) => onJudgeFormChange({ ...judgeForm, model_name: event.target.value })} placeholder="judge-model" /></div>
        </div>
        <div className="field"><label htmlFor="judge-endpoint">POST 服务地址</label><input id="judge-endpoint" className="input" type="url" required maxLength={500} value={judgeForm.endpoint} onChange={(event) => onJudgeFormChange({ ...judgeForm, endpoint: event.target.value })} placeholder="https://example.com/api/judge" /></div>
        <div className="field"><label htmlFor="judge-prompt">裁判指令</label><textarea id="judge-prompt" className="textarea" maxLength={10000} value={judgeForm.system_prompt} onChange={(event) => onJudgeFormChange({ ...judgeForm, system_prompt: event.target.value })} placeholder="说明评分维度、胜负标准和输出格式" /></div>
        <div className="form-grid">
          <div className="field"><label htmlFor="judge-timeout">超时秒数</label><input id="judge-timeout" className="input" type="number" min={10} max={300} required value={judgeForm.timeout_seconds} onChange={(event) => onJudgeFormChange({ ...judgeForm, timeout_seconds: Number(event.target.value) })} /></div>
          <label className="check-row" htmlFor="judge-active"><input id="judge-active" type="checkbox" checked={judgeForm.is_active} onChange={(event) => onJudgeFormChange({ ...judgeForm, is_active: event.target.checked })} />保存后设为当前启用裁判</label>
        </div>
        <div className="card-actions"><button className="button button-small" disabled={saving}>{saving ? "保存中…" : judgeTarget ? "保存裁判修改" : "创建裁判配置"}</button>{judgeTarget && <button type="button" className="button button-small button-secondary" onClick={() => onEditJudge()} disabled={saving}>取消编辑</button>}</div>
      </form>
      <div className="table-wrap" role="region" aria-label="AI 裁判配置表格" tabIndex={0} style={{ marginTop: 18 }}>
        <table><thead><tr><th>名称</th><th>模型与地址</th><th>超时</th><th>状态</th><th>操作</th></tr></thead><tbody>{judges.map((item) => <tr key={item.id}><td><strong>{item.name}</strong></td><td><strong>{item.model_name}</strong><small className="muted">{item.endpoint}</small></td><td>{item.timeout_seconds} 秒</td><td>{item.is_active ? "当前启用" : "备用"}</td><td><button className="text-button" disabled={saving} onClick={() => onEditJudge(item)}>编辑</button>{!item.is_active && <button className="text-button" disabled={saving} onClick={() => onActivateJudge(item)}>启用</button>}</td></tr>)}</tbody></table>
        {!judges.length && <div className="empty">尚未配置 AI 裁判；比赛结束时会进入管理员复核。</div>}
      </div>
      <h3 style={{ marginTop: 26 }}>辩手席位池</h3>
      <div className="table-wrap" role="region" aria-label="辩手席位池表格" tabIndex={0}>
        <table><thead><tr><th>名称</th><th>远程人设</th><th>音色</th><th>状态</th><th>操作</th></tr></thead><tbody>{agents.map((item) => <tr key={item.id}><td>{item.name}</td><td><code>{item.profile_key}</code></td><td>{item.voice_id}</td><td>{item.is_active ? "启用" : "停用"}</td><td><button className="text-button" disabled={saving} onClick={() => onPatchAgent(item.id, { is_active: !item.is_active })}>{item.is_active ? "停用" : "启用"}</button></td></tr>)}</tbody></table>
      </div>
      <div className="panel-title" style={{ marginTop: 30 }}><h3><Volume2 size={18} />阶段预设语音</h3><span className="badge">{audioCues.filter((item) => item.is_active).length} 条启用</span></div>
      <p className="muted">key 必须与自动流程阶段 key 完全一致。正式比赛只复用管理员提前制作的系统预设语音；未配置、已停用或文件缺失时将安全暂停，不会在比赛中临时合成另一种音色。</p>
      <form className="form-stack panel-subsection" onSubmit={onCreateAudioCue}>
        <div className="form-grid"><div className="field"><label htmlFor="audio-cue-key">阶段 key</label><input id="audio-cue-key" name="key" className="input" required minLength={2} maxLength={80} pattern="[a-z][a-z0-9_]{1,79}" placeholder="opening" /></div><div className="field"><label htmlFor="audio-cue-name">显示名称</label><input id="audio-cue-name" name="name" className="input" required maxLength={120} placeholder="开场规则播报" /></div></div>
        <div className="field"><label htmlFor="audio-cue-text">对应播报文本</label><textarea id="audio-cue-text" name="text" className="textarea" maxLength={2000} placeholder="用于核对预设音频内容；实际播放上传的 WAV。" /></div>
        <div className="field"><label htmlFor="audio-cue-file">PCM WAV 文件</label><input id="audio-cue-file" name="audio" className="input" type="file" accept="audio/wav,.wav" required /><small className="muted">单声道或双声道、16-bit PCM，0.1–600 秒，最大 20 MiB。</small></div>
        <div className="card-actions"><button className="button button-small" disabled={saving}>{saving ? "上传中…" : "上传预设语音"}</button></div>
      </form>
      <div className="table-wrap" role="region" aria-label="阶段预设语音表格" tabIndex={0} style={{ marginTop: 18 }}>
        <table><thead><tr><th>阶段 key</th><th>名称与文本</th><th>音频</th><th>状态</th><th>操作</th></tr></thead><tbody>{audioCues.map((item) => <tr key={item.id}><td><code>{item.key}</code></td><td><strong>{item.name}</strong><small className="muted">{item.text || "未填写文本说明"}</small></td><td>{item.audio_url ? "WAV 已上传" : "缺少音频"}</td><td>{item.is_active ? "启用" : "停用"}</td><td><button className="text-button" disabled={saving} onClick={() => onPatchAudioCue(item.id, { is_active: !item.is_active })}>{item.is_active ? "停用" : "启用"}</button></td></tr>)}</tbody></table>
        {!audioCues.length && <div className="empty">尚未上传阶段预设语音</div>}
      </div>
    </>
  );
}
