import Link from "next/link";
import type { FormEvent } from "react";
import { AlertTriangle, CheckCircle2, CircleDot, FlaskConical } from "lucide-react";

import { AdminPagination, type AdminPaginationState } from "@/components/admin/admin-pagination";
import { roomStatusLabel } from "@/lib/status-labels";

export type AdminRoomSummary = {
  id: string;
  code: string;
  topic: string;
  status: string;
  is_test_data: boolean;
  competition: { id?: string; slug?: string; name: string };
  current_stage: { key?: string; name: string } | null;
  updated_at?: string;
  paused_at?: string | null;
  failure_reason?: string;
  attention_reason?: string;
  connected_humans?: number;
};

type RoomsPanelProps = {
  rooms: AdminRoomSummary[];
  query: string;
  status: string;
  dataScope: string;
  pagination: AdminPaginationState;
  loading: boolean;
  saving: boolean;
  onQueryChange: (value: string) => void;
  onStatusChange: (value: string) => void;
  onDataScopeChange: (value: string) => void;
  onLoad: (page: number, query?: string, status?: string, dataScope?: string) => void;
  onMarkAsTestData: (room: AdminRoomSummary) => void;
};

const actionRequiredStatuses = new Set(["paused", "review_required"]);
const activeStatuses = new Set(["preparing", "running", "judging"]);
const finalStatuses = new Set(["completed", "terminated", "cancelled"]);

function roomStatusTone(room: AdminRoomSummary) {
  if (room.failure_reason || actionRequiredStatuses.has(room.status)) return "attention";
  if (activeStatuses.has(room.status)) return "active";
  if (finalStatuses.has(room.status)) return "final";
  return "neutral";
}

function lastActivityLabel(value?: string) {
  if (!value) return "更新时间未知";
  const time = new Date(value);
  if (Number.isNaN(time.getTime())) return "更新时间未知";
  return `更新于 ${time.toLocaleString("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  })}`;
}

function statusDisplayLabel(room: AdminRoomSummary) {
  if (room.status === "paused" && room.paused_at) {
    const pausedAt = new Date(room.paused_at).getTime();
    if (Number.isFinite(pausedAt) && Date.now() - pausedAt >= 60 * 60 * 1000) return "长期暂停";
  }
  return roomStatusLabel[room.status] || room.status;
}

function attentionDetail(room: AdminRoomSummary) {
  if (room.attention_reason === "review_required") return "赛果尚未发布，需要人工复核";
  if (room.attention_reason === "stale_paused") return "已暂停超过 1 小时且当前无真人在线";
  if (room.failure_reason) return room.failure_reason;
  if (room.attention_reason === "service_failure") return "外部服务失败，比赛等待人工处理";
  return "";
}

export function RoomsPanel({
  rooms,
  query,
  status,
  dataScope,
  pagination,
  loading,
  saving,
  onQueryChange,
  onStatusChange,
  onDataScopeChange,
  onLoad,
  onMarkAsTestData,
}: RoomsPanelProps) {
  const submitFilter = (event: FormEvent) => {
    event.preventDefault();
    onLoad(1, query, status, dataScope);
  };
  let attentionCount = 0;
  let activeCount = 0;
  let finalCount = 0;
  let qaCount = 0;
  rooms.forEach((room) => {
    const tone = roomStatusTone(room);
    if (tone === "attention") attentionCount += 1;
    else if (tone === "active") activeCount += 1;
    else if (tone === "final") finalCount += 1;
    if (room.is_test_data) qaCount += 1;
  });

  return (
    <>
      <div className="panel-title">
        <div>
          <h2>比赛监管</h2>
          <p className="admin-section-copy">优先处理暂停、失败和待复核比赛；QA 数据不会进入正式统计。</p>
        </div>
        <span className="muted">
          {loading && !rooms.length ? "正在载入房间…" : `共 ${pagination.total} 个房间`}
        </span>
      </div>
      {!!rooms.length && (
        <div className="room-ops-summary" aria-label="当前页比赛状态摘要">
          <span className={attentionCount ? "attention" : ""}><AlertTriangle size={15} />需处理 <strong>{attentionCount}</strong></span>
          <span><CircleDot size={15} />运行中 <strong>{activeCount}</strong></span>
          <span><CheckCircle2 size={15} />已结束 <strong>{finalCount}</strong></span>
          <span><FlaskConical size={15} />QA <strong>{qaCount}</strong></span>
          <small>当前页</small>
        </div>
      )}
      <form className="admin-filter-row admin-room-filter-row" onSubmit={submitFilter}>
        <input className="input" aria-label="搜索房间号或辩题" placeholder="搜索房间号或辩题" maxLength={100} value={query} onChange={(event) => onQueryChange(event.target.value)} />
        <select className="select" aria-label="筛选房间状态" value={status} onChange={(event) => onStatusChange(event.target.value)}>
          <option value="">全部状态</option><option value="lobby">大厅</option><option value="preparing">准备中</option>
          <option value="running">进行中</option><option value="paused">已暂停</option><option value="judging">裁判中</option>
          <option value="review_required">待复核</option><option value="completed">已完成</option><option value="terminated">已终止</option><option value="cancelled">已取消</option>
        </select>
        <select className="select" aria-label="筛选数据类型" value={dataScope} onChange={(event) => onDataScopeChange(event.target.value)}>
          <option value="">全部数据</option><option value="production">正式数据</option><option value="qa">QA 测试数据</option>
        </select>
        <button className="button button-small" disabled={loading}>{loading ? "筛选中…" : "筛选"}</button>
        <button
          type="button"
          className="button button-small button-secondary"
          disabled={loading || (!query && !status && !dataScope)}
          onClick={() => {
            onQueryChange("");
            onStatusChange("");
            onDataScopeChange("");
            onLoad(1, "", "", "");
          }}
        >
          清除筛选
        </button>
      </form>
      <div className="table-wrap admin-rooms-table-wrap" role="region" aria-label="比赛监管表格" aria-busy={loading} tabIndex={0}>
        <table className="admin-rooms-table">
          <thead><tr><th>房间与辩题</th><th>赛事</th><th>数据</th><th>状态</th><th>阶段与连接</th><th>最近活动</th><th>操作</th></tr></thead>
          <tbody>
            {rooms.map((room) => {
              const tone = roomStatusTone(room);
              const resultHref = `/rooms/${room.code}/result`;
              const attention = attentionDetail(room);
              return (
                <tr className={`admin-room-row ${tone}`} key={room.id}>
                  <td data-label="房间与辩题">
                    <span className="room-code">#{room.code}</span>
                    <strong className="admin-room-topic">{room.topic}</strong>
                  </td>
                  <td data-label="赛事">{room.competition.name}</td>
                  <td data-label="数据">{room.is_test_data ? <span className="badge closed">QA 测试</span> : <span className="room-data-production">正式</span>}</td>
                  <td data-label="状态"><span className={`room-status-chip ${tone}`}>{statusDisplayLabel(room)}</span>{attention && <small className="admin-room-failure">{attention}</small>}</td>
                  <td data-label="阶段与连接"><strong>{room.current_stage?.name || "房间大厅"}</strong>{room.connected_humans !== undefined && <small className="muted">{room.connected_humans} 位真人在线</small>}</td>
                  <td data-label="最近活动"><span className="admin-room-updated">{lastActivityLabel(room.updated_at)}</span></td>
                  <td data-label="操作">
                    <div className="admin-room-actions">
                      {room.status === "review_required" ? (
                        <Link className="admin-room-action-primary" href="/admin?module=reviews">立即复核</Link>
                      ) : tone === "attention" ? (
                        <Link className="admin-room-action-primary" aria-label={`处理房间 ${room.code} 的异常`} href={`/rooms/${room.code}/control`}>处理异常</Link>
                      ) : finalStatuses.has(room.status) ? (
                        <Link className="admin-room-action-primary" aria-label={`查看房间 ${room.code} 的结果`} href={resultHref}>查看结果</Link>
                      ) : (
                        <Link className="admin-room-action-primary" aria-label={`控制房间 ${room.code}`} href={`/rooms/${room.code}/control`}>控制</Link>
                      )}
                      {!finalStatuses.has(room.status) && <Link aria-label={`观战房间 ${room.code}`} href={`/rooms/${room.code}/watch`}>观战</Link>}
                      {!room.is_test_data && <button type="button" aria-label={`将房间 ${room.code} 标记为 QA`} className="text-button" disabled={saving} onClick={() => onMarkAsTestData(room)}>标记 QA</button>}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {!loading && !rooms.length && <div className="empty empty-guidance"><span>没有符合当前筛选条件的比赛</span><button type="button" className="button button-small button-secondary" disabled={!query && !status && !dataScope} onClick={() => { onQueryChange(""); onStatusChange(""); onDataScopeChange(""); onLoad(1, "", "", ""); }}>查看全部比赛</button></div>}
      </div>
      <AdminPagination label="比赛监管" page={pagination.page} pages={pagination.pages} loading={loading} onPageChange={onLoad} />
    </>
  );
}
