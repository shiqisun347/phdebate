import { AdminApp } from "./admin/AdminApp";
import { FeedbackProvider } from "./components/Feedback";
import { ConsolePage } from "./pages/ConsolePage";
import { HostPage } from "./pages/HostPage";
import { NavigationPage } from "./pages/NavigationPage";
import { ScreenPage } from "./pages/ScreenPage";
import { VotePage } from "./pages/VotePage";
import { useVersionGuard } from "./realtime/useVersionGuard";

export function App() {
  // 版本守卫：任何旧标签页在新版部署后自动刷新，杜绝旧缓存 bundle 仍在运行。
  useVersionGuard();
  const params = new URLSearchParams(window.location.search);
  const path = window.location.pathname;
  const matchId = routeMatchId(path, params);

  let page;
  if (path.startsWith("/admin")) return <AdminApp matchId={matchId} />;
  else if (path.startsWith("/host")) page = <HostPage matchId={matchId} />;
  else if (path.startsWith("/screen")) page = <ScreenPage matchId={matchId} />;
  else if (path.startsWith("/console")) {
    const speakerId = path.split("/").filter(Boolean)[1] ?? "spk_aff_3";
    page = <ConsolePage matchId={matchId} speakerId={speakerId} />;
  } else if (path.startsWith("/vote")) page = <VotePage matchId={matchId} />;
  else page = <NavigationPage />;

  return <FeedbackProvider>{page}</FeedbackProvider>;
}

function routeMatchId(path: string, params: URLSearchParams): string {
  const queryMatchId = params.get("match_id");
  if (queryMatchId) return queryMatchId;
  const legacyVoteMatchId = path.match(/^\/vote\/([^/]+)/)?.[1];
  if (legacyVoteMatchId) return decodeURIComponent(legacyVoteMatchId);
  return "current";
}
