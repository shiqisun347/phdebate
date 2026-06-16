import * as React from "react";
import { useMatch } from "../../realtime/useMatch";
import { listMatches } from "../../api/client";
import type { MatchList, MatchSnapshot, RealtimeMessage } from "../../types/contracts";

interface AdminData {
  matchId: string;
  snapshot: MatchSnapshot | null;
  matchList: MatchList | null;
  socketStatus: "connecting" | "open" | "closed" | "reconnecting";
  lastEvent: RealtimeMessage | null;
  loadError: string | null;
  refresh: () => Promise<void>;
  refreshList: () => Promise<void>;
}

const AdminDataContext = React.createContext<AdminData | null>(null);

export function useAdminData() {
  const ctx = React.useContext(AdminDataContext);
  if (!ctx) throw new Error("useAdminData must be used within AdminDataProvider");
  return ctx;
}

export function AdminDataProvider({ matchId, children }: { matchId: string; children: React.ReactNode }) {
  const { snapshot, socketStatus, lastEvent, loadError, refresh } = useMatch(matchId, "admin");
  const [matchList, setMatchList] = React.useState<MatchList | null>(null);

  const refreshList = React.useCallback(async () => {
    try {
      setMatchList(await listMatches());
    } catch {
      /* surfaced elsewhere */
    }
  }, []);

  // Load the match list once on mount. Refreshing it on EVERY realtime event
  // floods the network during a running match (clock/ASR ticks) and re-renders
  // the whole tree — modules call refreshList() explicitly after create/switch/
  // delete, so we only auto-refresh on registry-level events here.
  React.useEffect(() => {
    void refreshList();
  }, [refreshList]);

  React.useEffect(() => {
    const t = lastEvent?.type ?? "";
    if (t.startsWith("match.")) void refreshList();
  }, [lastEvent, refreshList]);

  const value: AdminData = {
    matchId,
    snapshot,
    matchList,
    socketStatus,
    lastEvent,
    loadError,
    refresh,
    refreshList,
  };
  return <AdminDataContext.Provider value={value}>{children}</AdminDataContext.Provider>;
}
