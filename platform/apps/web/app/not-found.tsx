import Link from "next/link";

export default function NotFound() {
  return (
    <div className="page-shell">
      <section
        className="panel"
        style={{ maxWidth: 720, margin: "64px auto", padding: "clamp(28px, 5vw, 52px)" }}
      >
        <span className="eyebrow">404 · Page not found</span>
        <h1 style={{ margin: "14px 0 12px", fontSize: "clamp(32px, 6vw, 52px)" }}>
          没有找到这个页面
        </h1>
        <p className="hero-copy" style={{ margin: 0, fontSize: 16 }}>
          链接可能已过期或地址输入有误。所有赛事、房间和观战入口现在都从赛事大厅进入。
        </p>
        <div className="hero-actions" style={{ flexWrap: "wrap" }}>
          <Link className="button" href="/">
            返回赛事大厅
          </Link>
          <Link className="button button-secondary" href="/rankings">
            查看排行榜
          </Link>
        </div>
      </section>
    </div>
  );
}
