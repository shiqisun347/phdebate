import Link from "next/link";
import { AlertTriangle, ArrowRight, CheckCircle2 } from "lucide-react";
import type { ReactNode } from "react";

export type HealthCheck = {
  ok: boolean;
  latency_ms?: number;
  age_seconds?: number;
  age_hours?: number;
  free_percent?: number;
  warning?: boolean;
  status?: string;
  warning_percent?: number;
  failure_percent?: number;
  current?: string;
  expected?: string;
  message?: string;
  skipped?: boolean;
  workers?: number;
  queued_messages?: number;
  dead_letters?: number;
  configured_max_active?: number;
  ready_endpoints?: number;
  required_endpoints?: number;
  service?: string;
  release?: string;
  source_commit?: string;
  source_tree?: string;
};

export type AdminDashboard = {
  counts: {
    users: number;
    competitions: number;
    live_rooms: number;
    review_required: number;
  };
  rooms: { code: string; topic: string; status: string; updated_at: string }[];
  providers: Record<
    string,
    {
      endpoint: string;
      enabled: boolean;
      healthy: boolean | null;
      status: string;
      latency_ms?: number;
      message?: string;
      model_name?: string;
      max_active?: number;
    }
  >;
  leaderboard: unknown[];
  system_health: {
    ok: boolean;
    checks: Record<string, HealthCheck>;
    checked_at: string;
  };
};

const healthLabels: Record<string, string> = {
  database: "数据库",
  schema: "数据库结构",
  redis: "Redis",
  engine: "比赛引擎",
  worker: "后台任务",
  lighttts: "LightTTS 语音合成",
  moss_tts_realtime: "MOSS 实时语音合成",
  funasr: "FunASR 语音识别",
  storage: "磁盘空间",
  backup: "数据库备份",
  agent_backup: "Agent 数据库备份",
  release: "API 发布来源",
};

export const providerDisplayLabels: Record<string, string> = {
  agent: "辩手 Agent",
  funasr: "FunASR 语音识别",
  lighttts: "兼容 LightTTS",
  judge: "AI 裁判",
};

export const healthDetail = (value: HealthCheck, key?: string) => {
  if (value.skipped) return "开发环境跳过";
  if (value.ready_endpoints !== undefined) {
    return `${value.ready_endpoints}/${value.required_endpoints ?? value.ready_endpoints} 个实时端点已就绪`;
  }
  if (value.workers !== undefined) {
    return `${value.message ? `${value.message} · ` : ""}${value.workers} 个 Worker · 排队 ${value.queued_messages ?? 0} · 死信 ${value.dead_letters ?? 0}`;
  }
  if (value.configured_max_active !== undefined) {
    const response = value.message || (value.latency_ms !== undefined ? `响应 ${value.latency_ms} ms` : "检查完成");
    return `${response} · 推理并发上限 ${value.configured_max_active}`;
  }
  if (key === "release" && value.release) return `${value.release} · ${value.source_commit?.slice(0, 12) || "未知提交"}`;
  if (value.status === "failed" && value.age_hours !== undefined) return `最近执行失败 · 上次成功在 ${value.age_hours} 小时前`;
  if (value.status === "running" && value.age_hours !== undefined) return `正在备份 · 上次成功在 ${value.age_hours} 小时前`;
  if (value.message || value.latency_ms !== undefined) return `${value.message || "响应"} ${value.latency_ms ?? ""} ms`.trim();
  if (value.age_seconds !== undefined) return `心跳 ${value.age_seconds} 秒前`;
  if (value.age_hours !== undefined) return `备份 ${value.age_hours} 小时前`;
  if (value.free_percent !== undefined) return `剩余 ${value.free_percent}% · ${value.warning_percent ?? 20}% 告警 / ${value.failure_percent ?? 10}% 阻断`;
  if (key === "schema") return value.current === value.expected ? "结构版本已同步" : "结构版本需要升级";
  if (value.current) return value.current;
  return "检查完成";
};

function ServiceCard({ status, children }: { status: "online" | "warning" | ""; children: ReactNode }) {
  return (
    <div className="service-card">
      <i className={`status-dot ${status}`} />
      <div>{children}</div>
    </div>
  );
}

export function SystemOverview({ dashboard }: { dashboard: AdminDashboard }) {
  const visibleProviders = Object.entries(dashboard.providers).filter(
    ([key, value]) => key !== "lighttts" || value.enabled,
  );
  const failedHealthChecks = Object.entries(dashboard.system_health.checks).filter(([, value]) => !value.ok);
  const warningHealthChecks = Object.entries(dashboard.system_health.checks).filter(([, value]) => value.ok && value.warning);
  const attentionRooms = dashboard.rooms.filter((room) => ["paused", "review_required"].includes(room.status));
  const needsAttention = failedHealthChecks.length > 0 || warningHealthChecks.length > 0 || dashboard.counts.review_required > 0 || attentionRooms.length > 0;

  return (
    <>
      <div className="panel-title">
        <h2>系统总览</h2>
        <span className="muted">实时业务统计</span>
      </div>
      <div className="stats-grid">
        <div className="stat-card"><span className="muted">注册用户</span><strong>{dashboard.counts.users}</strong></div>
        <div className="stat-card"><span className="muted">赛事类型</span><strong>{dashboard.counts.competitions}</strong></div>
        <div className="stat-card"><span className="muted">活跃比赛</span><strong>{dashboard.counts.live_rooms}</strong></div>
        <div className="stat-card"><span className="muted">等待复核</span><strong>{dashboard.counts.review_required}</strong></div>
      </div>
      <div className={`admin-ops-callout ${needsAttention ? "attention" : "ready"}`} role="status">
        <span className="admin-ops-callout-icon" aria-hidden="true">{needsAttention ? <AlertTriangle size={20} /> : <CheckCircle2 size={20} />}</span>
        <div>
          <strong>{needsAttention ? "有运营事项需要处理" : "当前没有待处理事项"}</strong>
          <small>
            {needsAttention
              ? `${failedHealthChecks.length} 项服务异常 · ${warningHealthChecks.length} 项运维预警 · ${attentionRooms.length} 场近期比赛暂停或待处理 · ${dashboard.counts.review_required} 场赛果待复核`
              : "服务检查正常，近期比赛没有暂停或待复核记录。"}
          </small>
        </div>
        {needsAttention && <nav aria-label="待处理事项快捷入口"><Link href="/admin?module=rooms">比赛监管<ArrowRight size={14} /></Link>{dashboard.counts.review_required > 0 && <Link href="/admin?module=reviews">结果复核<ArrowRight size={14} /></Link>}</nav>}
      </div>
      <h3 style={{ marginTop: 28 }}>平台就绪检查</h3>
      <div className="service-grid">
        {Object.entries(dashboard.system_health.checks).map(([key, value]) => (
          <ServiceCard key={key} status={!value.ok || value.warning ? "warning" : "online"}>
            <strong>{healthLabels[key] || key} · {!value.ok ? "异常" : value.warning ? "预警" : "正常"}</strong>
            <small>{healthDetail(value, key)}</small>
          </ServiceCard>
        ))}
      </div>
      <h3 style={{ marginTop: 28 }}>外部与语音服务</h3>
      <div className="service-grid">
        {visibleProviders.map(([key, value]) => (
          <ServiceCard
            key={key}
            status={value.healthy === true ? "online" : value.enabled ? "warning" : ""}
          >
            <strong>
              {providerDisplayLabels[key] || key.toUpperCase()} · {value.enabled
                ? value.healthy === true ? "可达" : value.healthy === false ? "异常" : "待检测"
                : "未配置"}
            </strong>
            <small>{value.endpoint || "未配置"}{value.latency_ms !== undefined ? ` · ${value.latency_ms} ms` : ""}</small>
          </ServiceCard>
        ))}
      </div>
    </>
  );
}
