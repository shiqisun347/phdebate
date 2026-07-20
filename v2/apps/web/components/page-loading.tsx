export function PageLoading({ label }: { label: string }) {
  return (
    <div className="loading-screen" role="status" aria-live="polite" aria-busy="true">
      <div className="page-loading-card">
        <span className="page-loading-mark" aria-hidden="true">辩</span>
        <span className="page-loading-copy">
          <strong>{label}</strong>
          <small>正在同步最新比赛数据</small>
        </span>
        <span className="page-loading-progress" aria-hidden="true"><i /></span>
      </div>
    </div>
  );
}
