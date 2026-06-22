import * as React from "react";
import { Menu, Wifi, WifiOff, ChevronRight } from "lucide-react";
import "./admin.css";
import { cn } from "./lib/cn";
import { AuthPrompt } from "../components/AuthPrompt";
import { ToastProvider } from "./lib/toast";
import { AdminDataProvider, useAdminData } from "./lib/data";
import { NAV, findItem, type ModuleId } from "./nav";
import { MODULES } from "./modules";

function useHashModule(): [ModuleId, (id: ModuleId) => void] {
  const read = (): ModuleId => {
    const raw = window.location.hash.replace(/^#\/?/, "") as ModuleId;
    return findItem(raw) ? raw : "overview";
  };
  const [id, setId] = React.useState<ModuleId>(read);
  React.useEffect(() => {
    const onHash = () => setId(read());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const navigate = React.useCallback((next: ModuleId) => {
    window.location.hash = `/${next}`;
    setId(next);
  }, []);
  return [id, navigate];
}

function MatchBadge() {
  const { snapshot, socketStatus } = useAdminData();
  const online = socketStatus === "open";
  return (
    <div className="flex items-center gap-3">
      <div className="hidden text-right md:block">
        <p className="text-xs text-muted-foreground">当前比赛</p>
        <p className="max-w-[220px] truncate text-sm font-medium text-foreground">
          {snapshot ? (snapshot.match.id ? (snapshot.match.title || "未命名比赛") : "未创建比赛") : "未加载"}
        </p>
      </div>
      <span
        className={cn(
          "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium",
          online ? "bg-success/12 text-success" : "bg-warning/15 text-warning"
        )}
        title={`实时连接：${socketStatus}`}
      >
        {online ? <Wifi className="size-3.5" /> : <WifiOff className="size-3.5" />}
        {online ? "已连接" : "重连中"}
      </span>
    </div>
  );
}

function Sidebar({
  active,
  onNavigate,
  onClose,
}: {
  active: ModuleId;
  onNavigate: (id: ModuleId) => void;
  onClose?: () => void;
}) {
  return (
    <nav className="flex h-full w-64 shrink-0 flex-col gap-1 overflow-y-auto bg-sidebar px-3 py-4 text-sidebar-foreground">
      <div className="flex items-center gap-2.5 px-2.5 pb-4">
        <div className="flex size-9 items-center justify-center rounded-lg bg-sidebar-accent text-white">
          <MonitorIcon />
        </div>
        <div>
          <p className="text-sm font-semibold leading-tight">人机辩论赛</p>
          <p className="text-xs text-sidebar-foreground/60">控制台 Admin</p>
        </div>
      </div>
      {NAV.map((zone) => (
        <div key={zone.key} className="mb-2">
          <div className="px-2.5 pb-1.5 pt-2">
            <p className="text-[11px] font-semibold uppercase tracking-wider text-sidebar-foreground/50">
              {zone.title}
            </p>
            <p className="text-[11px] text-sidebar-foreground/40">{zone.hint}</p>
          </div>
          {zone.items.map((item) => {
            const Icon = item.icon;
            const isActive = active === item.id;
            return (
              <button
                key={item.id}
                onClick={() => {
                  onNavigate(item.id);
                  onClose?.();
                }}
                className={cn(
                  "group flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors",
                  isActive
                    ? "bg-sidebar-accent text-white shadow-sm"
                    : "text-sidebar-foreground/80 hover:bg-white/5 hover:text-sidebar-foreground"
                )}
              >
                <Icon className="size-4 shrink-0" />
                <span className="flex-1 text-left">{item.label}</span>
                {isActive && <ChevronRight className="size-3.5 opacity-70" />}
              </button>
            );
          })}
        </div>
      ))}
    </nav>
  );
}

function MonitorIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <rect x="2" y="3" width="20" height="14" rx="2" />
      <path d="M8 21h8M12 17v4" />
    </svg>
  );
}

function Shell() {
  const [active, navigate] = useHashModule();
  const [mobileOpen, setMobileOpen] = React.useState(false);
  const { snapshot, loadError } = useAdminData();
  const item = findItem(active);
  const ModuleComponent = MODULES[active];
  const noMatch = Boolean(snapshot && !snapshot.match.id);

  if (!snapshot && loadError) return <AuthPrompt role="admin" message={loadError} />;
  if (!snapshot) return <div className="flex h-screen items-center justify-center text-sm text-muted-foreground">正在加载技术后台...</div>;

  return (
    <div className="flex h-screen overflow-hidden">
      {/* desktop sidebar */}
      <div className="hidden lg:block">
        <Sidebar active={active} onNavigate={navigate} />
      </div>
      {/* mobile drawer */}
      {mobileOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div className="absolute inset-0 bg-black/50" onClick={() => setMobileOpen(false)} />
          <div className="absolute left-0 top-0 h-full animate-slide-up">
            <Sidebar active={active} onNavigate={navigate} onClose={() => setMobileOpen(false)} />
          </div>
        </div>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-16 shrink-0 items-center gap-3 border-b border-border bg-card px-4 md:px-6">
          <button
            onClick={() => setMobileOpen(true)}
            className="rounded-md p-2 text-muted-foreground hover:bg-accent lg:hidden"
          >
            <Menu className="size-5" />
          </button>
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-lg font-semibold text-foreground">{item?.label}</h1>
            <p className="truncate text-xs text-muted-foreground">{item?.desc}</p>
          </div>
          <MatchBadge />
        </header>
        <main className="flex-1 overflow-y-auto bg-background p-4 md:p-6">
          <div className="mx-auto max-w-6xl">
            {noMatch && active !== "matches" && (
              <div className="mb-4 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-warning/40 bg-warning/10 px-4 py-3 text-sm text-foreground">
                <span>还没有比赛。请先在「比赛管理」新建比赛后再进行其它操作。</span>
                <button
                  className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground"
                  onClick={() => navigate("matches")}
                >
                  去比赛管理新建
                </button>
              </div>
            )}
            <ModuleComponent />
          </div>
        </main>
      </div>
    </div>
  );
}

export function AdminApp({ matchId }: { matchId: string }) {
  return (
    <div className="admin-shell h-screen">
      <ToastProvider>
        <AdminDataProvider matchId={matchId}>
          <Shell />
        </AdminDataProvider>
      </ToastProvider>
    </div>
  );
}
