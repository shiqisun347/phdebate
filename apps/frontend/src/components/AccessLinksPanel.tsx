import { Copy, Download, ExternalLink, Printer, QrCode, RefreshCw, Upload } from "lucide-react";
import QRCode from "qrcode";
import { useEffect, useMemo, useState } from "react";
import type { Speaker } from "../types/contracts";
import { seatLabel, sideLabel } from "../state/format";

interface AccessLinksPanelProps {
  matchId: string;
  title: string;
  topic: string;
  speakers: Speaker[];
}

type LinkItem = {
  id: string;
  title: string;
  subtitle: string;
  href: string;
};

type TokenBundle = {
  hostToken?: string;
  screenToken?: string;
  sharedSpeakerToken?: string;
  speakerTokens?: Record<string, string>;
};

const STORAGE_KEYS = {
  baseUrl: "phdebate_access_base_url",
  hostToken: "phdebate_share_host_token",
  screenToken: "phdebate_share_screen_token",
  sharedSpeakerToken: "phdebate_share_speaker_token"
};

export function AccessLinksPanel({ matchId, title, topic, speakers }: AccessLinksPanelProps) {
  const [baseUrl, setBaseUrl] = useStoredValue(STORAGE_KEYS.baseUrl, window.location.origin);
  const [hostToken, setHostToken] = useStoredValue(STORAGE_KEYS.hostToken, "");
  const [screenToken, setScreenToken] = useStoredValue(STORAGE_KEYS.screenToken, "");
  const [sharedSpeakerToken, setSharedSpeakerToken] = useStoredValue(STORAGE_KEYS.sharedSpeakerToken, "");
  const [speakerTokens, setSpeakerTokens] = useState<Record<string, string>>(() => loadSpeakerTokens());
  const [tokenImportText, setTokenImportText] = useState("");
  const [tokenFileSnippet, setTokenFileSnippet] = useState("");
  const [toolMessage, setToolMessage] = useState("");

  useEffect(() => {
    window.localStorage.setItem("phdebate_share_speaker_tokens", JSON.stringify(speakerTokens));
  }, [speakerTokens]);

  const humanSpeakers = useMemo(() => speakers.filter((speaker) => speaker.speaker_type === "human"), [speakers]);
  const activeSpeakerTokens = useMemo(() => {
    const entries = humanSpeakers
      .map((speaker) => [speaker.id, speakerTokens[speaker.id]?.trim() ?? ""] as const)
      .filter(([, token]) => Boolean(token));
    return Object.fromEntries(entries);
  }, [humanSpeakers, speakerTokens]);
  const envSnippet = useMemo(
    () => createEnvSnippet({ hostToken, screenToken, sharedSpeakerToken, speakerTokens: activeSpeakerTokens }),
    [activeSpeakerTokens, hostToken, screenToken, sharedSpeakerToken]
  );
  const hasAnyToken = Boolean(hostToken || screenToken || sharedSpeakerToken || Object.keys(activeSpeakerTokens).length);
  useEffect(() => {
    let cancelled = false;
    if (!hasAnyToken) {
      setTokenFileSnippet("");
      return () => {
        cancelled = true;
      };
    }
    createTokenFileSnippet({ hostToken, screenToken, sharedSpeakerToken, speakerTokens: activeSpeakerTokens })
      .then((snippet) => {
        if (!cancelled) setTokenFileSnippet(snippet);
      })
      .catch(() => {
        if (!cancelled) setTokenFileSnippet("当前浏览器无法生成 SHA-256 token 文件，请改用环境变量方式。");
      });
    return () => {
      cancelled = true;
    };
  }, [activeSpeakerTokens, hasAnyToken, hostToken, screenToken, sharedSpeakerToken]);
  const links = useMemo<LinkItem[]>(() => {
    const root = normalizeBaseUrl(baseUrl);
    return [
      {
        id: "host",
        title: "主持导播台",
        subtitle: "主持人现场流程设备",
        href: withParams(root, "/host", { token: hostToken })
      },
      {
        id: "admin",
        title: "技术后台",
        subtitle: "技术统筹/管理员设备",
        href: withParams(root, "/admin", { token: hostToken })
      },
      {
        id: "screen",
        title: "大屏",
        subtitle: "投屏电脑只读入口",
        href: withParams(root, "/screen", { token: screenToken })
      },
      {
        id: "vote",
        title: "学生投票",
        subtitle: "可直接上屏或打印",
        href: `${root}/vote`
      },
      ...humanSpeakers.map((speaker) => ({
        id: speaker.id,
        title: `${sideLabel(speaker.side)}${seatLabel(speaker.seat)} · ${speaker.name}`,
        subtitle: "辩手控制台",
        href: withParams(root, `/console/${speaker.id}`, {
          token: speakerTokens[speaker.id] || sharedSpeakerToken
        })
      }))
    ];
  }, [baseUrl, hostToken, humanSpeakers, matchId, screenToken, sharedSpeakerToken, speakerTokens]);

  function updateSpeakerToken(speakerId: string, token: string) {
    setSpeakerTokens((current) => ({ ...current, [speakerId]: token }));
  }

  function generateTokenSuite() {
    const nextSpeakerTokens = Object.fromEntries(
      humanSpeakers.map((speaker) => [speaker.id, createToken(`spk_${speaker.side}_${speaker.seat}`)])
    );
    setHostToken(createToken("host"));
    setScreenToken(createToken("screen"));
    setSharedSpeakerToken("");
    setSpeakerTokens(nextSpeakerTokens);
    setToolMessage("已生成新的本地 token 清单；复制环境变量并重启生产服务后生效。");
  }

  async function copyEnvSnippet() {
    try {
      await window.navigator.clipboard.writeText(envSnippet);
      setToolMessage("已复制 PHDEBATE 环境变量片段。");
    } catch {
      setToolMessage("复制失败，请手动选择环境变量片段。");
    }
  }

  async function copyTokenFileSnippet() {
    try {
      await window.navigator.clipboard.writeText(tokenFileSnippet);
      setToolMessage("已复制哈希 token 文件内容；保存为 storage/tokens.json 后设置 PHDEBATE_TOKEN_FILE=storage/tokens.json。");
    } catch {
      setToolMessage("复制失败，请手动选择 token 文件内容。");
    }
  }

  function downloadTokenFile() {
    const blob = new Blob([tokenFileSnippet], { type: "application/json;charset=utf-8" });
    const url = window.URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "phdebate-current-tokens.json";
    link.click();
    window.URL.revokeObjectURL(url);
    setToolMessage("已下载哈希 token 文件；现场推荐保存为 storage/tokens.json 并设置 PHDEBATE_TOKEN_FILE。");
  }

  function importTokens() {
    try {
      const imported = parseTokenImport(tokenImportText);
      if (imported.hostToken !== undefined) setHostToken(imported.hostToken);
      if (imported.screenToken !== undefined) setScreenToken(imported.screenToken);
      if (imported.sharedSpeakerToken !== undefined) setSharedSpeakerToken(imported.sharedSpeakerToken);
      if (imported.speakerTokens) {
        setSpeakerTokens((current) => ({ ...current, ...imported.speakerTokens }));
      }
      setToolMessage("已导入 token 配置，链接、二维码和打印清单已同步更新。");
    } catch (error) {
      setToolMessage(error instanceof Error ? error.message : "导入失败，请检查格式。");
    }
  }

  return (
    <div className="panel access-panel">
      <div className="panel-head">
        <span><QrCode size={16} />现场入口分发</span>
        <button type="button" onClick={() => window.print()}><Printer size={14} />打印清单</button>
      </div>
      <div className="access-config">
        <label>
          <span>现场主机地址</span>
          <input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="http://192.168.1.10:8000" />
        </label>
        <label>
          <span>管理/主持 token</span>
          <input type="password" value={hostToken} onChange={(event) => setHostToken(event.target.value)} placeholder="仅发给主持人设备" />
        </label>
        <label>
          <span>大屏只读 token</span>
          <input type="password" value={screenToken} onChange={(event) => setScreenToken(event.target.value)} placeholder="只读大屏 token" />
        </label>
        <label>
          <span>辩手共享 token</span>
          <input type="password" value={sharedSpeakerToken} onChange={(event) => setSharedSpeakerToken(event.target.value)} placeholder="可选；独立 token 优先" />
        </label>
      </div>
      <div className="access-token-tools">
        <div className="access-tool-head">
          <strong>现场 token 工具</strong>
          <span>生成或导入后会立即更新本页链接；后端生产鉴权可使用环境变量，或使用只含 SHA-256 哈希的 token 文件。</span>
        </div>
        <div className="access-tool-actions">
          <button type="button" onClick={generateTokenSuite}><RefreshCw size={14} />生成/轮换 token</button>
          <button type="button" onClick={copyEnvSnippet} disabled={!hasAnyToken}><Copy size={14} />复制环境变量</button>
          <button type="button" onClick={copyTokenFileSnippet} disabled={!hasAnyToken || !tokenFileSnippet}><Copy size={14} />复制 token 文件</button>
          <button type="button" onClick={downloadTokenFile} disabled={!hasAnyToken || !tokenFileSnippet}><Download size={14} />下载 token 文件</button>
        </div>
        <textarea
          className="access-token-snippet"
          readOnly
          value={hasAnyToken ? envSnippet : "填写或生成 token 后，这里会显示 PHDEBATE_* 环境变量片段。"}
          aria-label="PHDEBATE 环境变量片段"
        />
        <textarea
          className="access-token-snippet"
          readOnly
          value={hasAnyToken ? tokenFileSnippet : "填写或生成 token 后，这里会显示只含哈希的 token 文件内容。"}
          aria-label="PHDEBATE 哈希 token 文件内容"
        />
        <div className="access-import-grid">
          <textarea
            value={tokenImportText}
            onChange={(event) => setTokenImportText(event.target.value)}
            placeholder={'粘贴 JSON 或 PHDEBATE_* 环境变量片段，例如：\n{"hostToken":"host","screenToken":"screen","speakerTokens":{"spk_aff_3":"speaker"}}'}
            aria-label="批量导入 token 配置"
          />
          <button type="button" onClick={importTokens}><Upload size={14} />导入 token</button>
        </div>
        {toolMessage ? <p className="access-tool-message">{toolMessage}</p> : null}
      </div>
      <div className="speaker-token-list">
        {humanSpeakers.map((speaker) => (
          <label key={speaker.id}>
            <span>{sideLabel(speaker.side)}{seatLabel(speaker.seat)} · {speaker.name}</span>
            <input
              type="password"
              value={speakerTokens[speaker.id] ?? ""}
              onChange={(event) => updateSpeakerToken(speaker.id, event.target.value)}
              placeholder="该辩手独立 token"
            />
          </label>
        ))}
      </div>
      <p className="access-print-note">打印清单包含完整 token，仅限现场设备和辩手本人分发。</p>
      <div className="access-link-grid">
        {links.map((item) => <AccessLinkCard item={item} key={item.id} />)}
      </div>
      <PrintableAccessSheet title={title} topic={topic} matchId={matchId} links={links} />
    </div>
  );
}

function AccessLinkCard({ item }: { item: LinkItem }) {
  const [qrDataUrl, setQrDataUrl] = useState("");
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let cancelled = false;
    QRCode.toDataURL(item.href, {
      errorCorrectionLevel: "M",
      margin: 1,
      width: 168,
      color: {
        dark: "#10151c",
        light: "#ffffff"
      }
    }).then((url) => {
      if (!cancelled) setQrDataUrl(url);
    });
    return () => {
      cancelled = true;
    };
  }, [item.href]);

  async function copy() {
    try {
      await window.navigator.clipboard.writeText(item.href);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  }

  return (
    <article className="access-link-card">
      <div className="access-qr">{qrDataUrl ? <img src={qrDataUrl} alt={`${item.title} QR`} /> : <QrCode size={40} />}</div>
      <div className="access-link-body">
        <strong>{item.title}</strong>
        <span>{item.subtitle}</span>
        <code>{redactToken(item.href)}</code>
        <div className="button-row">
          <button type="button" onClick={copy}><Copy size={14} />{copied ? "已复制" : "复制"}</button>
          <a href={item.href} target="_blank" rel="noreferrer"><ExternalLink size={14} />打开</a>
        </div>
      </div>
    </article>
  );
}

function PrintableAccessSheet({
  title,
  topic,
  matchId,
  links
}: {
  title: string;
  topic: string;
  matchId: string;
  links: LinkItem[];
}) {
  return (
    <section className="print-sheet" aria-hidden="true">
      <header>
        <h1>{title}</h1>
        <p>{topic}</p>
        <span>当前比赛现场入口</span>
      </header>
      <div className="print-link-grid">
        {links.map((item) => <PrintableLinkItem item={item} key={item.id} />)}
      </div>
      <footer>包含现场访问 token，仅限比赛现场分发。</footer>
    </section>
  );
}

function PrintableLinkItem({ item }: { item: LinkItem }) {
  const [qrDataUrl, setQrDataUrl] = useState("");

  useEffect(() => {
    let cancelled = false;
    QRCode.toDataURL(item.href, {
      errorCorrectionLevel: "M",
      margin: 1,
      width: 192,
      color: {
        dark: "#000000",
        light: "#ffffff"
      }
    }).then((url) => {
      if (!cancelled) setQrDataUrl(url);
    });
    return () => {
      cancelled = true;
    };
  }, [item.href]);

  return (
    <article className="print-link-card">
      <div className="print-qr">{qrDataUrl ? <img src={qrDataUrl} alt={`${item.title} QR`} /> : null}</div>
      <div>
        <strong>{item.title}</strong>
        <span>{item.subtitle}</span>
        <code>{item.href}</code>
      </div>
    </article>
  );
}

function useStoredValue(key: string, initial: string): [string, (value: string) => void] {
  const [value, setValue] = useState(() => window.localStorage.getItem(key) ?? initial);
  function update(next: string) {
    setValue(next);
    window.localStorage.setItem(key, next);
  }
  return [value, update];
}

function loadSpeakerTokens(): Record<string, string> {
  try {
    const raw = window.localStorage.getItem("phdebate_share_speaker_tokens");
    return raw ? JSON.parse(raw) as Record<string, string> : {};
  } catch {
    return {};
  }
}

function normalizeBaseUrl(value: string): string {
  const trimmed = value.trim() || window.location.origin;
  return trimmed.replace(/\/+$/, "");
}

function withParams(root: string, path: string, params: Record<string, string>) {
  const url = new URL(path, root);
  for (const [key, value] of Object.entries(params)) {
    if (value) url.searchParams.set(key, value);
  }
  return url.toString();
}

function createToken(prefix: string): string {
  const normalizedPrefix = prefix.replace(/[^a-z0-9_]/gi, "_").toLowerCase();
  if (window.crypto.randomUUID) {
    return `${normalizedPrefix}_${window.crypto.randomUUID().replace(/-/g, "").slice(0, 20)}`;
  }
  const bytes = new Uint8Array(12);
  window.crypto.getRandomValues(bytes);
  return `${normalizedPrefix}_${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

function createEnvSnippet(bundle: TokenBundle): string {
  const lines = ["PHDEBATE_AUTH_REQUIRED=1"];
  if (bundle.hostToken) {
    lines.push(`PHDEBATE_ADMIN_PASSWORD=${shellQuote(bundle.hostToken)}`);
    lines.push(`PHDEBATE_HOST_PASSWORD=${shellQuote(bundle.hostToken)}`);
  }
  if (bundle.screenToken) {
    lines.push(`PHDEBATE_SCREEN_TOKEN=${shellQuote(bundle.screenToken)}`);
  }
  if (bundle.sharedSpeakerToken) {
    lines.push(`PHDEBATE_SPEAKER_TOKEN=${shellQuote(bundle.sharedSpeakerToken)}`);
  }
  if (bundle.speakerTokens && Object.keys(bundle.speakerTokens).length) {
    lines.push(`PHDEBATE_SPEAKER_TOKENS=${shellQuote(JSON.stringify(bundle.speakerTokens))}`);
  }
  return lines.join(" \\\n");
}

async function createTokenFileSnippet(bundle: TokenBundle): Promise<string> {
  const hostHash = bundle.hostToken ? await tokenHash(bundle.hostToken) : null;
  const speakerHashes: Record<string, string[]> = {};
  for (const [speakerId, token] of Object.entries(bundle.speakerTokens ?? {})) {
    if (token) speakerHashes[speakerId] = [await tokenHash(token)];
  }
  const tokenFile = {
    version: 1,
    hash_algorithm: "sha256",
    generated_at: new Date().toISOString(),
    admin_hashes: hostHash ? [hostHash] : [],
    host_hashes: hostHash ? [hostHash] : [],
    screen_hashes: bundle.screenToken ? [await tokenHash(bundle.screenToken)] : [],
    speaker_shared_hashes: bundle.sharedSpeakerToken ? [await tokenHash(bundle.sharedSpeakerToken)] : [],
    speaker_hashes: speakerHashes
  };
  return JSON.stringify(tokenFile, null, 2);
}

async function tokenHash(token: string): Promise<string> {
  if (!window.crypto?.subtle) {
    throw new Error("crypto.subtle unavailable");
  }
  const digest = await window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
  const hex = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `sha256:${hex}`;
}

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, "'\\''")}'`;
}

function parseTokenImport(raw: string): TokenBundle {
  const trimmed = raw.trim();
  if (!trimmed) {
    throw new Error("请先粘贴 JSON 或 PHDEBATE_* 环境变量片段。");
  }
  if (trimmed.startsWith("{")) {
    const bundle = normalizeJsonBundle(JSON.parse(trimmed));
    if (!hasBundleTokens(bundle)) throw new Error("JSON 中未识别到可导入的 token 字段。");
    return bundle;
  }
  const env = parseEnvSnippet(trimmed);
  const bundle: TokenBundle = {};
  bundle.hostToken =
    env.PHDEBATE_ADMIN_PASSWORD ||
    env.PHDEBATE_ADMIN_TOKEN ||
    env.PHDEBATE_HOST_PASSWORD ||
    env.PHDEBATE_HOST_TOKEN;
  bundle.screenToken = env.PHDEBATE_SCREEN_TOKEN;
  bundle.sharedSpeakerToken = env.PHDEBATE_SPEAKER_TOKEN;
  if (env.PHDEBATE_SPEAKER_TOKENS) {
    bundle.speakerTokens = parseSpeakerTokens(env.PHDEBATE_SPEAKER_TOKENS);
  }
  if (!hasBundleTokens(bundle)) {
    throw new Error("未识别到可导入的 token 字段。");
  }
  return bundle;
}

function hasBundleTokens(bundle: TokenBundle): boolean {
  return Boolean(
    bundle.hostToken ||
    bundle.screenToken ||
    bundle.sharedSpeakerToken ||
    (bundle.speakerTokens && Object.keys(bundle.speakerTokens).length)
  );
}

function normalizeJsonBundle(value: unknown): TokenBundle {
  if (!value || typeof value !== "object") {
    throw new Error("JSON 必须是对象。");
  }
  const record = value as Record<string, unknown>;
  const speakerValue = record.speakerTokens ?? record.PHDEBATE_SPEAKER_TOKENS;
  return {
    hostToken: stringValue(record.hostToken ?? record.adminToken ?? record.PHDEBATE_ADMIN_PASSWORD ?? record.PHDEBATE_HOST_PASSWORD),
    screenToken: stringValue(record.screenToken ?? record.PHDEBATE_SCREEN_TOKEN),
    sharedSpeakerToken: stringValue(record.sharedSpeakerToken ?? record.PHDEBATE_SPEAKER_TOKEN),
    speakerTokens: parseSpeakerTokenValue(speakerValue)
  };
}

function parseEnvSnippet(raw: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const line of raw.split(/\r?\n/)) {
    let clean = line.trim();
    if (!clean || clean.startsWith("#")) continue;
    if (clean.endsWith("\\")) clean = clean.slice(0, -1).trim();
    const equalIndex = clean.indexOf("=");
    if (equalIndex < 0) continue;
    const key = clean.slice(0, equalIndex).trim();
    if (!key.startsWith("PHDEBATE_")) continue;
    result[key] = unquoteValue(clean.slice(equalIndex + 1).trim());
  }
  return result;
}

function parseSpeakerTokenValue(value: unknown): Record<string, string> | undefined {
  if (!value) return undefined;
  if (typeof value === "string") return parseSpeakerTokens(value);
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .flatMap(([key, token]) => {
        const normalizedToken = stringValue(token);
        return normalizedToken ? [[key, normalizedToken] as const] : [];
      });
    return entries.length ? Object.fromEntries(entries) : undefined;
  }
  return undefined;
}

function parseSpeakerTokens(raw: string): Record<string, string> {
  const trimmed = raw.trim();
  if (!trimmed) return {};
  if (trimmed.startsWith("{")) {
    return parseSpeakerTokenValue(JSON.parse(trimmed)) ?? {};
  }
  const entries = trimmed
    .split(",")
    .map((pair) => pair.split(":"))
    .filter((pair): pair is [string, string] => pair.length >= 2)
    .map(([speakerId, token]) => [speakerId.trim(), token.trim()] as const)
    .filter(([speakerId, token]) => Boolean(speakerId && token));
  return Object.fromEntries(entries);
}

function unquoteValue(raw: string): string {
  let value = raw.trim();
  if ((value.startsWith("'") && value.endsWith("'")) || (value.startsWith('"') && value.endsWith('"'))) {
    value = value.slice(1, -1);
  }
  return value;
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function redactToken(href: string): string {
  const url = new URL(href);
  if (url.searchParams.has("token")) {
    url.searchParams.set("token", "...");
  }
  return url.toString();
}
