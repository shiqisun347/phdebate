import type { FormEvent } from "react";
import type { User } from "@/lib/types";
import { AdminPagination, type AdminPaginationState } from "@/components/admin/admin-pagination";

const userRoleLabels: Record<User["role"], string> = {
  user: "参赛者",
  system_admin: "系统管理员",
};

type UsersPanelProps = {
  currentUserId?: string;
  users: User[];
  query: string;
  pagination: AdminPaginationState;
  loading: boolean;
  saving: boolean;
  onQueryChange: (query: string) => void;
  onSearch: (page: number, query?: string) => void;
  onPasswordReset: (user: User) => void;
  onPatch: (user: User, patch: Record<string, unknown>) => void;
};

export function UsersPanel({
  currentUserId,
  users,
  query,
  pagination,
  loading,
  saving,
  onQueryChange,
  onSearch,
  onPasswordReset,
  onPatch,
}: UsersPanelProps) {
  const submitSearch = (event: FormEvent) => {
    event.preventDefault();
    onSearch(1, query);
  };

  return (
    <>
      <div className="panel-title">
        <h2>用户管理</h2>
        <span className="muted">
          {loading && !users.length
            ? "正在载入用户…"
            : `共 ${pagination.total} 位用户 · 安全变更会撤销相关会话`}
        </span>
      </div>
      <form className="admin-filter-row" onSubmit={submitSearch}>
        <input
          className="input"
          aria-label="搜索真实姓名或账号"
          placeholder="搜索真实姓名或账号"
          maxLength={64}
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
        />
        <button className="button button-small" disabled={loading}>{loading ? "搜索中…" : "搜索"}</button>
        <button
          type="button"
          className="button button-small button-secondary"
          disabled={loading || !query}
          onClick={() => {
            onQueryChange("");
            onSearch(1, "");
          }}
        >
          清除搜索
        </button>
      </form>
      <div className="table-wrap" role="region" aria-label="用户管理表格" aria-busy={loading} tabIndex={0}>
        <table>
          <thead><tr><th>真实姓名</th><th>登录账号</th><th>角色</th><th>数据类型</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            {users.map((item) => (
              <tr key={item.id}>
                <td><strong>{item.real_name}</strong>{item.id === currentUserId && <small className="muted"> · 当前账号</small>}</td>
                <td>@{item.account}</td>
                <td>{userRoleLabels[item.role]}</td>
                <td>{item.is_test_account ? <span className="badge closed">QA 测试</span> : "正式数据"}</td>
                <td>{item.is_active ? "正常" : "已停用"}</td>
                <td>
                  {item.id === currentUserId ? <span className="muted">受保护</span> : (
                    <>
                      <button type="button" aria-label={`重置 ${item.real_name} 的密码`} className="text-button" disabled={saving} onClick={() => onPasswordReset(item)}>重置密码</button>
                      <button type="button" aria-label={`${item.is_test_account ? "恢复" : "标记"} ${item.real_name} 为${item.is_test_account ? "正式" : "QA"}账号`} className="text-button" disabled={saving} onClick={() => onPatch(item, { is_test_account: !item.is_test_account })}>
                        {item.is_test_account ? "恢复为正式账号" : "标记为 QA 账号"}
                      </button>
                      <button type="button" aria-label={`${item.is_active ? "停用" : "恢复"}账号 ${item.real_name}`} className="text-button" disabled={saving} onClick={() => onPatch(item, { is_active: !item.is_active })}>
                        {item.is_active ? "停用" : "恢复"}
                      </button>
                      <button type="button" aria-label={`${item.role === "system_admin" ? "取消" : "授予"} ${item.real_name} 的管理员权限`} className="text-button" disabled={saving} onClick={() => onPatch(item, { role: item.role === "system_admin" ? "user" : "system_admin" })}>
                        {item.role === "system_admin" ? "取消管理员" : "设为管理员"}
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!loading && !users.length && <div className="empty">没有符合条件的用户</div>}
      </div>
      <AdminPagination label="用户管理" page={pagination.page} pages={pagination.pages} loading={loading} onPageChange={onSearch} />
    </>
  );
}
