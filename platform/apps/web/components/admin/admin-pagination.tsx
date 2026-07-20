export type AdminPaginationState = {
  page: number;
  page_size: number;
  total: number;
  pages: number;
};

type AdminPaginationProps = {
  label: string;
  page: number;
  pages: number;
  loading: boolean;
  onPageChange: (page: number) => void;
};

export function AdminPagination({
  label,
  page,
  pages,
  loading,
  onPageChange,
}: AdminPaginationProps) {
  return (
    <nav className="pagination-controls" aria-label={`${label}分页`}>
      <span aria-live="polite" aria-atomic="true">
        第 {page} / {pages} 页
      </span>
      <button
        className="button button-small button-secondary"
        disabled={loading || page <= 1}
        onClick={() => onPageChange(page - 1)}
        type="button"
      >
        上一页
      </button>
      <button
        className="button button-small button-secondary"
        disabled={loading || page >= pages}
        onClick={() => onPageChange(page + 1)}
        type="button"
      >
        下一页
      </button>
    </nav>
  );
}
