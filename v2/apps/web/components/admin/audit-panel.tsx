import { AdminPagination, type AdminPaginationState } from "@/components/admin/admin-pagination";
import { auditActionLabels, auditTargetLabels, type Audit } from "@/components/admin/admin-module-types";

type AuditPanelProps = {
  items: Audit[];
  query: string;
  pagination: AdminPaginationState;
  loading: boolean;
  onQueryChange: (query: string) => void;
  onLoad: (page: number, query?: string) => void;
};

export default function AuditPanel({ items, query, pagination, loading, onQueryChange, onLoad }: AuditPanelProps) {
  return (
    <>
      <div className="panel-title">
        <h2>管理员审计日志</h2>
        <span className="muted">{loading && !items.length ? "正在载入审计记录…" : `共 ${pagination.total} 条`}</span>
      </div>
      <form className="admin-filter-row" onSubmit={(event) => { event.preventDefault(); onLoad(1, query); }}>
        <input className="input" aria-label="搜索操作或目标" placeholder="搜索操作、目标类型或 ID" maxLength={120} value={query} onChange={(event) => onQueryChange(event.target.value)} />
        <button className="button button-small" disabled={loading}>{loading ? "搜索中…" : "搜索"}</button>
      </form>
      <div className="table-wrap" role="region" aria-label="审计日志表格" tabIndex={0}>
        <table>
          <thead><tr><th>时间</th><th>管理员</th><th>操作</th><th>目标</th><th>详情</th></tr></thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.id}>
                <td>{new Date(item.created_at).toLocaleString("zh-CN")}</td>
                <td>{item.actor_name}</td>
                <td>{auditActionLabels[item.action] || item.action}</td>
                <td>{auditTargetLabels[item.target_type] || item.target_type} · {item.target_id}</td>
                <td><details><summary>查看</summary><pre className="audit-json">{JSON.stringify(item.payload, null, 2)}</pre></details></td>
              </tr>
            ))}
          </tbody>
        </table>
        {!loading && !items.length && <div className="empty">没有符合条件的审计记录</div>}
      </div>
      <AdminPagination label="审计日志" page={pagination.page} pages={pagination.pages} loading={loading} onPageChange={(page) => onLoad(page)} />
    </>
  );
}
