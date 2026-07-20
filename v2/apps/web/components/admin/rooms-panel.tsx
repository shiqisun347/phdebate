import Link from "next/link";
import type { FormEvent } from "react";
import { AdminPagination, type AdminPaginationState } from "@/components/admin/admin-pagination";
import { roomStatusLabel } from "@/lib/status-labels";
import type { Room } from "@/lib/types";

type RoomsPanelProps = {
  rooms: Room[];
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
  onMarkAsTestData: (room: Room) => void;
};

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

  return (
    <>
      <div className="panel-title">
        <h2>比赛监管</h2>
        <span className="muted">
          {loading && !rooms.length ? "正在载入房间…" : `共 ${pagination.total} 个房间`}
        </span>
      </div>
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
      <div className="table-wrap" role="region" aria-label="比赛监管表格" aria-busy={loading} tabIndex={0}>
        <table>
          <thead><tr><th>房间</th><th>辩题</th><th>赛事</th><th>数据类型</th><th>状态</th><th>阶段</th><th>操作</th></tr></thead>
          <tbody>
            {rooms.map((room) => (
              <tr key={room.id}>
                <td className="room-code">#{room.code}</td><td>{room.topic}</td><td>{room.competition.name}</td>
                <td>{room.is_test_data ? <span className="badge closed">QA 测试</span> : "正式数据"}</td>
                <td>{roomStatusLabel[room.status] || room.status}</td><td>{room.current_stage?.name || "大厅"}</td>
                <td>
                  <Link aria-label={`控制房间 ${room.code}`} href={`/rooms/${room.code}/control`}>控制</Link> · <Link aria-label={`观战房间 ${room.code}`} href={`/rooms/${room.code}/watch`}>观战</Link>
                  {!room.is_test_data && <> · <button type="button" aria-label={`将房间 ${room.code} 标记为 QA`} className="text-button" disabled={saving} onClick={() => onMarkAsTestData(room)}>标记 QA</button></>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!loading && !rooms.length && <div className="empty">没有符合条件的房间</div>}
      </div>
      <AdminPagination label="比赛监管" page={pagination.page} pages={pagination.pages} loading={loading} onPageChange={onLoad} />
    </>
  );
}
