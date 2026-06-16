import { useEffect, useState } from "react";
import { patchIntegrationConfig } from "../api/client";
import type { IntegrationConfig, IntegrationSection } from "../types/contracts";

type SectionDraft = {
  enabled: boolean;
  endpoint: string;
  extra: string; // lang (asr) or voice (tts)
  app_id: string;
  api_key: string;
  api_secret: string;
};

function draftFrom(section: IntegrationSection, kind: "asr" | "tts"): SectionDraft {
  return {
    enabled: section.enabled,
    endpoint: section.endpoint ?? "",
    extra: (kind === "asr" ? section.lang : section.voice) ?? "",
    app_id: "",
    api_key: "",
    api_secret: ""
  };
}

function secretPlaceholder(configured: boolean): string {
  return configured ? "已配置（留空不修改）" : "未配置";
}

/**
 * 需求 2.md：ASR/TTS 管理 —— 可添加 API 设置、切换是否启用。
 * 密钥只写不回显；留空表示不修改。
 */
export function IntegrationConfigPanel({ matchId, config, onSaved }: { matchId: string; config: IntegrationConfig; onSaved?: () => void }) {
  const [asr, setAsr] = useState<SectionDraft>(() => draftFrom(config.asr, "asr"));
  const [tts, setTts] = useState<SectionDraft>(() => draftFrom(config.tts, "tts"));
  const [saving, setSaving] = useState<"asr" | "tts" | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  // 当后端配置变化（如其他端保存）时刷新草稿的开关/端点显示
  useEffect(() => {
    setAsr((prev) => ({ ...prev, enabled: config.asr.enabled, endpoint: config.asr.endpoint ?? "", extra: config.asr.lang ?? prev.extra }));
    setTts((prev) => ({ ...prev, enabled: config.tts.enabled, endpoint: config.tts.endpoint ?? "", extra: config.tts.voice ?? prev.extra }));
  }, [config.asr.enabled, config.asr.endpoint, config.asr.lang, config.tts.enabled, config.tts.endpoint, config.tts.voice]);

  async function save(kind: "asr" | "tts") {
    const draft = kind === "asr" ? asr : tts;
    const secrets: Record<string, string> = {};
    if (draft.app_id.trim()) secrets.app_id = draft.app_id.trim();
    if (draft.api_key.trim()) secrets.api_key = draft.api_key.trim();
    if (draft.api_secret.trim()) secrets.api_secret = draft.api_secret.trim();
    const payload: Record<string, unknown> = { enabled: draft.enabled, endpoint: draft.endpoint.trim(), secrets };
    payload[kind === "asr" ? "lang" : "voice"] = draft.extra.trim();
    setSaving(kind);
    setMessage(null);
    try {
      await patchIntegrationConfig(matchId, { [kind]: payload });
      setMessage(`${kind.toUpperCase()} 配置已保存`);
      if (kind === "asr") setAsr((p) => ({ ...p, app_id: "", api_key: "", api_secret: "" }));
      else setTts((p) => ({ ...p, app_id: "", api_key: "", api_secret: "" }));
      onSaved?.();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "保存失败");
    } finally {
      setSaving(null);
    }
  }

  function renderSection(kind: "asr" | "tts", draft: SectionDraft, setDraft: (next: SectionDraft) => void, section: IntegrationSection) {
    const extraLabel = kind === "asr" ? "语种 lang" : "发音人 vcn";
    const extraPlaceholder = kind === "asr" ? "autodialect" : "x6_lingfeiyi_pro";
    return (
      <div className="integration-section">
        <div className="integration-head">
          <strong>{kind === "asr" ? "ASR 实时转写" : "TTS 语音合成"}</strong>
          <label className="integration-toggle">
            <input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />
            <span>{draft.enabled ? "已启用" : "已停用"}</span>
          </label>
        </div>
        <label className="wide"><span>WebSocket Endpoint</span>
          <input value={draft.endpoint} placeholder="wss://..." onChange={(e) => setDraft({ ...draft, endpoint: e.target.value })} />
        </label>
        <label><span>{extraLabel}</span>
          <input value={draft.extra} placeholder={extraPlaceholder} onChange={(e) => setDraft({ ...draft, extra: e.target.value })} />
        </label>
        <label><span>APPID</span>
          <input value={draft.app_id} placeholder={secretPlaceholder(section.secrets.app_id.configured)} onChange={(e) => setDraft({ ...draft, app_id: e.target.value })} />
        </label>
        <label><span>APIKey</span>
          <input value={draft.api_key} placeholder={secretPlaceholder(section.secrets.api_key.configured)} onChange={(e) => setDraft({ ...draft, api_key: e.target.value })} />
        </label>
        <label><span>APISecret</span>
          <input value={draft.api_secret} placeholder={secretPlaceholder(section.secrets.api_secret.configured)} onChange={(e) => setDraft({ ...draft, api_secret: e.target.value })} />
        </label>
        <button type="button" disabled={saving === kind} onClick={() => void save(kind)}>
          {saving === kind ? "保存中…" : `保存 ${kind.toUpperCase()} 配置`}
        </button>
      </div>
    );
  }

  return (
    <div className="integration-config">
      <p className="integration-note">密钥只写不回显，留空表示不修改；停用后该服务自动降级（不调用真实接口）。ASR/TTS 共用同一套讯飞密钥。</p>
      <div className="integration-grid">
        {renderSection("asr", asr, setAsr, config.asr)}
        {renderSection("tts", tts, setTts, config.tts)}
      </div>
      {message && <div className="integration-message">{message}</div>}
    </div>
  );
}
