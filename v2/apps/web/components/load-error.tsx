"use client";

import Link from "next/link";

export function LoadError({ message, retry }: { message: string; retry?: () => void }) {
  return (
    <div className="loading-screen">
      <div className="load-error-card panel" role="alert">
        <span className="eyebrow">Unable to load</span>
        <h2>页面暂时无法打开</h2>
        <p>{message}</p>
        <div className="hero-actions">
          {retry && <button type="button" className="button" onClick={retry}>重新尝试</button>}
          <Link href="/" className="button button-secondary">返回赛事大厅</Link>
        </div>
      </div>
    </div>
  );
}
