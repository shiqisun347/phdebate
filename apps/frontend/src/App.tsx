import { AdminPage } from "./pages/AdminPage";
import { ConsolePage } from "./pages/ConsolePage";
import { ScreenPage } from "./pages/ScreenPage";
import { VotePage } from "./pages/VotePage";

export function App() {
  const params = new URLSearchParams(window.location.search);
  const matchId = params.get("match_id") ?? window.location.pathname.match(/\/vote\/([^/]+)/)?.[1] ?? "match_001";
  const path = window.location.pathname;

  if (path.startsWith("/admin")) return <AdminPage matchId={matchId} />;
  if (path.startsWith("/console")) {
    const speakerId = path.split("/").filter(Boolean)[1] ?? "spk_aff_3";
    return <ConsolePage matchId={matchId} speakerId={speakerId} />;
  }
  if (path.startsWith("/vote")) return <VotePage matchId={matchId} />;
  return <ScreenPage matchId={matchId} />;
}

