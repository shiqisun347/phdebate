"use client";

import dynamic from "next/dynamic";
import {
  Activity,
  Bot,
  FileCheck2,
  Gauge,
  HardDrive,
  Library,
  Radio,
  RefreshCw,
  Shield,
  Users,
  Workflow,
} from "lucide-react";
import { useRouter } from "next/navigation";
import {
  type KeyboardEvent as ReactKeyboardEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { apiFetch } from "@/lib/api";
import { competitionDisplayName } from "@/lib/primary-competition";
import { useSession } from "@/lib/use-session";
import type { Competition, Season, User } from "@/lib/types";
import { LoadError } from "@/components/load-error";
import { PageLoading } from "@/components/page-loading";
import type { AdminPaginationState } from "@/components/admin/admin-pagination";
import { RoomsPanel, type AdminRoomSummary } from "@/components/admin/rooms-panel";
import { SystemOverview, type AdminDashboard } from "@/components/admin/system-overview";
import { UsersPanel } from "@/components/admin/users-panel";
import type {
  AgentProfileSummary,
  AdminSpeechCorrection,
  ArchiveStatus,
  AudioCue,
  Audit,
  AutomationTemplate,
  DataQualityStatus,
  JudgeProfile,
  MediaStatus,
  ProviderConfig,
  Review,
} from "@/components/admin/admin-module-types";

const moduleFallback = () => <div className="empty" role="status">正在载入管理模块…</div>;
const CompetitionsModule = dynamic(() => import("@/components/admin/modules/competitions-module"), { loading: moduleFallback });
const AutomationModule = dynamic(() => import("@/components/admin/automation-panel"), { loading: moduleFallback });
const AgentsModule = dynamic(() => import("@/components/admin/modules/agents-module"), { loading: moduleFallback });
const MediaModule = dynamic(() => import("@/components/admin/modules/media-module"), { loading: moduleFallback });
const ReviewsModule = dynamic(() => import("@/components/admin/reviews-panel"), { loading: moduleFallback });
const AuditModule = dynamic(() => import("@/components/admin/audit-panel"), { loading: moduleFallback });
const TopicDialog = dynamic(() => import("@/components/admin/admin-dialogs").then((module) => module.TopicDialog));
const PasswordResetDialog = dynamic(() => import("@/components/admin/admin-dialogs").then((module) => module.PasswordResetDialog));
const TemplateVersionDialog = dynamic(() => import("@/components/admin/admin-dialogs").then((module) => module.TemplateVersionDialog));
const ReviewDialog = dynamic(() => import("@/components/admin/admin-dialogs").then((module) => module.ReviewDialog));
const tabs = [
  { id: "overview", label: "系统总览", icon: Gauge },
  { id: "rooms", label: "比赛监管", icon: Radio },
  { id: "users", label: "用户管理", icon: Users },
  { id: "competitions", label: "赛事与题库", icon: Library },
  { id: "automation", label: "自动流程", icon: Workflow },
  { id: "agents", label: "AI 与语音", icon: Bot },
  { id: "media", label: "媒体存储", icon: HardDrive },
  { id: "reviews", label: "结果复核", icon: FileCheck2 },
  { id: "audit", label: "审计日志", icon: Shield },
] as const;
type AdminTab = (typeof tabs)[number]["id"];
function adminTabFromLocation(): AdminTab {
  if (typeof window === "undefined") return "overview";
  const requested = new URLSearchParams(window.location.search).get("module");
  return tabs.some((item) => item.id === requested)
    ? (requested as AdminTab)
    : "overview";
}

function persistAdminTab(tabId: AdminTab) {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  if (tabId === "overview") url.searchParams.delete("module");
  else url.searchParams.set("module", tabId);
  window.history.replaceState(
    window.history.state,
    "",
    `${url.pathname}${url.search}${url.hash}`,
  );
}

const emptyPagination: AdminPaginationState = {
  page: 1,
  page_size: 50,
  total: 0,
  pages: 1,
};

export default function AdminPage() {
  const router = useRouter();
  const { user, loading } = useSession();
  const [tab, setTab] = useState<AdminTab>("overview");
  const [dash, setDash] = useState<AdminDashboard | null>(null);
  const [users, setUsers] = useState<User[]>([]);
  const [userQuery, setUserQuery] = useState("");
  const [userPagination, setUserPagination] =
    useState<AdminPaginationState>(emptyPagination);
  const [competitions, setCompetitions] = useState<Competition[]>([]);
  const [seasons, setSeasons] = useState<Season[]>([]);
  const [seasonForm, setSeasonForm] = useState({
    name: "",
    slug: "",
    starts_at: "",
    ends_at: "",
  });
  const [rooms, setRooms] = useState<AdminRoomSummary[]>([]);
  const [roomQuery, setRoomQuery] = useState("");
  const [roomStatus, setRoomStatus] = useState("");
  const [roomDataScope, setRoomDataScope] = useState("");
  const [roomPagination, setRoomPagination] =
    useState<AdminPaginationState>(emptyPagination);
  const [agents, setAgents] = useState<AgentProfileSummary[]>([]);
  const [audioCues, setAudioCues] = useState<AudioCue[]>([]);
  const [judges, setJudges] = useState<JudgeProfile[]>([]);
  const [providerConfigs, setProviderConfigs] = useState<ProviderConfig[]>([]);
  const [providerDrafts, setProviderDrafts] = useState<Record<string, ProviderConfig>>({});
  const [judgeTarget, setJudgeTarget] = useState<JudgeProfile | null>(null);
  const [judgeForm, setJudgeForm] = useState({
    name: "",
    endpoint: "",
    model_name: "",
    system_prompt: "",
    timeout_seconds: 120,
    is_active: true,
  });
  const [media, setMedia] = useState<MediaStatus | null>(null);
  const [archives, setArchives] = useState<ArchiveStatus | null>(null);
  const [dataQuality, setDataQuality] = useState<DataQualityStatus | null>(null);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [recentReviews, setRecentReviews] = useState<Review[]>([]);
  const [speechCorrections, setSpeechCorrections] = useState<AdminSpeechCorrection[]>([]);
  const [recentSpeechCorrections, setRecentSpeechCorrections] = useState<AdminSpeechCorrection[]>([]);
  const [audit, setAudit] = useState<Audit[]>([]);
  const [auditQuery, setAuditQuery] = useState("");
  const [auditPagination, setAuditPagination] =
    useState<AdminPaginationState>(emptyPagination);
  const [usersLoading, setUsersLoading] = useState(false);
  const [roomsLoading, setRoomsLoading] = useState(false);
  const [auditLoading, setAuditLoading] = useState(false);
  const usersRequestSequence = useRef(0);
  const roomsRequestSequence = useRef(0);
  const auditRequestSequence = useRef(0);
  const pendingActions = useRef(new Set<string>());
  const loadedTabs = useRef(new Set<AdminTab>());
  const pendingTabLoads = useRef(new Map<AdminTab, Promise<void>>());
  const loadTabHandler = useRef<
    ((tabId: AdminTab, force?: boolean) => Promise<void>) | null
  >(null);
  const restoredModule = useRef(false);
  const [loadingTabs, setLoadingTabs] = useState<Set<AdminTab>>(
    () => new Set(),
  );
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [topicCompetition, setTopicCompetition] = useState<Competition | null>(
    null,
  );
  const [topicTitle, setTopicTitle] = useState("");
  const [passwordResetTarget, setPasswordResetTarget] = useState<User | null>(
    null,
  );
  const [passwordResetForm, setPasswordResetForm] = useState({
    new_password: "",
    confirm_password: "",
  });
  const [reviewTarget, setReviewTarget] = useState<Review | null>(null);
  const [retryingReviewIds, setRetryingReviewIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [reviewMode, setReviewMode] = useState<"approve" | "correct">(
    "approve",
  );
  const [reviewForm, setReviewForm] = useState({
    winner: "aff",
    affirmative_score: 85,
    negative_score: 82,
    reasoning: "",
  });
  const [saving, setSaving] = useState(false);
  const [automationTemplates, setAutomationTemplates] = useState<
    AutomationTemplate[]
  >([]);
  const [templateTarget, setTemplateTarget] =
    useState<AutomationTemplate | null>(null);
  const [templateName, setTemplateName] = useState("");
  const [templateStages, setTemplateStages] = useState("");
  const [templateCompetitionIds, setTemplateCompetitionIds] = useState<
    string[]
  >([]);
  useEffect(() => {
    if (!loading && user?.role !== "system_admin") router.replace("/?notice=admin-required");
  }, [user, loading, router]);
  const load = useCallback(async () => {
    try {
      setError("");
      const d = await apiFetch<AdminDashboard>("/api/admin/dashboard");
      setDash(d);
      loadedTabs.current.add("overview");
    } catch (err) {
      setError(err instanceof Error ? err.message : "管理数据载入失败");
    }
  }, []);
  async function refreshDashboard() {
    const data = await apiFetch<AdminDashboard>("/api/admin/dashboard");
    setDash(data);
    loadedTabs.current.add("overview");
  }
  async function refreshCompetitionData() {
    const [competitionData, seasonData] = await Promise.all([
      apiFetch<{ items: Competition[] }>("/api/admin/competitions"),
      apiFetch<{ items: Season[] }>("/api/admin/seasons"),
    ]);
    setCompetitions(competitionData.items);
    setSeasons(seasonData.items);
    loadedTabs.current.add("competitions");
  }
  async function refreshAutomationData() {
    const [templateData, competitionData] = await Promise.all([
      apiFetch<{ items: AutomationTemplate[] }>(
        "/api/admin/automation-templates",
      ),
      apiFetch<{ items: Competition[] }>("/api/admin/competitions"),
    ]);
    setAutomationTemplates(templateData.items);
    setCompetitions(competitionData.items);
    loadedTabs.current.add("automation");
  }
  async function refreshAgents() {
    const agentData = await apiFetch<{ items: typeof agents }>(
      "/api/admin/agents",
    );
    setAgents(agentData.items);
  }
  async function refreshJudges() {
    const judgeData = await apiFetch<{ items: JudgeProfile[] }>(
      "/api/admin/judges",
    );
    setJudges(judgeData.items);
  }
  async function refreshProviders() {
    const providerData = await apiFetch<{ items: ProviderConfig[] }>(
      "/api/admin/providers",
    );
    setProviderConfigs(providerData.items);
    setProviderDrafts(
      Object.fromEntries(
        providerData.items.map((item) => [item.kind, item]),
      ),
    );
  }
  async function refreshAudioCues() {
    const audioCueData = await apiFetch<{ items: AudioCue[] }>(
      "/api/admin/audio-cues",
    );
    setAudioCues(audioCueData.items);
  }
  async function refreshReviewData() {
    const [reviewData, correctionData] = await Promise.all([
      apiFetch<{ items: Review[]; recent: Review[] }>("/api/admin/reviews"),
      apiFetch<{ items: AdminSpeechCorrection[]; recent: AdminSpeechCorrection[] }>("/api/admin/speech-corrections"),
    ]);
    setReviews(reviewData.items);
    setRecentReviews(reviewData.recent || []);
    setSpeechCorrections(correctionData.items);
    setRecentSpeechCorrections(correctionData.recent || []);
    loadedTabs.current.add("reviews");
  }

  async function reviewSpeechCorrection(
    item: AdminSpeechCorrection,
    decision: "approve" | "reject",
    reason: string,
  ) {
    setSaving(true);
    setError("");
    setNotice("");
    try {
      await apiFetch(`/api/admin/speech-corrections/${item.id}/${decision}`, {
        method: "POST",
        body: JSON.stringify({ expected_updated_at: item.updated_at, reason }),
      });
      await Promise.all([refreshReviewData(), refreshAuditIfLoaded()]);
      setNotice(decision === "approve" ? "发言文字修正已批准并写入审计记录。" : "发言文字修正申请已拒绝。");
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "发言修正审核失败";
      setError(message);
      throw caught;
    } finally {
      setSaving(false);
    }
  }
  useEffect(() => {
    if (user?.role === "system_admin") void load();
  }, [load, user?.role]);
  async function loadUsers(page = 1, q = userQuery) {
    const requestSequence = ++usersRequestSequence.current;
    setUsersLoading(true);
    try {
      const data = await apiFetch<{ items: User[]; pagination: AdminPaginationState }>(
        `/api/admin/users?page=${page}&page_size=50&q=${encodeURIComponent(q)}`,
      );
      if (requestSequence !== usersRequestSequence.current) return;
      setUsers(data.items);
      setUserPagination(data.pagination);
      loadedTabs.current.add("users");
    } catch (err) {
      if (requestSequence !== usersRequestSequence.current) return;
      setError(err instanceof Error ? err.message : "用户列表载入失败");
    } finally {
      if (requestSequence === usersRequestSequence.current) setUsersLoading(false);
    }
  }
  async function loadRooms(page = 1, q = roomQuery, status = roomStatus, dataScope = roomDataScope) {
    const requestSequence = ++roomsRequestSequence.current;
    setRoomsLoading(true);
    try {
      const data = await apiFetch<{ items: AdminRoomSummary[]; pagination: AdminPaginationState }>(
        `/api/admin/rooms?page=${page}&page_size=50&q=${encodeURIComponent(q)}&status=${encodeURIComponent(status)}&data_scope=${encodeURIComponent(dataScope)}`,
      );
      if (requestSequence !== roomsRequestSequence.current) return;
      setRooms(data.items);
      setRoomPagination(data.pagination);
      loadedTabs.current.add("rooms");
    } catch (err) {
      if (requestSequence !== roomsRequestSequence.current) return;
      setError(err instanceof Error ? err.message : "房间列表载入失败");
    } finally {
      if (requestSequence === roomsRequestSequence.current) setRoomsLoading(false);
    }
  }
  async function loadAudit(page = 1, q = auditQuery) {
    const requestSequence = ++auditRequestSequence.current;
    setAuditLoading(true);
    try {
      const data = await apiFetch<{ items: Audit[]; pagination: AdminPaginationState }>(
        `/api/admin/audit?page=${page}&page_size=50&q=${encodeURIComponent(q)}`,
      );
      if (requestSequence !== auditRequestSequence.current) return;
      setAudit(data.items);
      setAuditPagination(data.pagination);
      loadedTabs.current.add("audit");
    } catch (err) {
      if (requestSequence !== auditRequestSequence.current) return;
      setError(err instanceof Error ? err.message : "审计日志载入失败");
    } finally {
      if (requestSequence === auditRequestSequence.current) setAuditLoading(false);
    }
  }
  async function refreshAuditIfLoaded() {
    if (loadedTabs.current.has("audit")) await loadAudit(1, auditQuery);
  }
  async function markRoomAsTestData(room: AdminRoomSummary) {
    if (!window.confirm(`确认将房间 #${room.code} 标记为 QA 测试数据？该房间将永久退出正式排行榜和研究数据。`)) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      await apiFetch(`/api/admin/rooms/${room.code}/data-scope`, {
        method: "PATCH",
        body: JSON.stringify({ is_test_data: true }),
      });
      await Promise.all([
        loadRooms(roomPagination.page, roomQuery, roomStatus, roomDataScope),
        refreshAuditIfLoaded(),
      ]);
      setNotice(`房间 #${room.code} 已标记为 QA 测试数据。`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "房间数据类型保存失败");
    } finally {
      setSaving(false);
    }
  }
  async function patchUser(id: string, patch: Record<string, unknown>) {
    const actionKey = `user:${id}`;
    if (pendingActions.current.has(actionKey)) return;
    pendingActions.current.add(actionKey);
    setSaving(true);
    setError("");
    try {
      await apiFetch(`/api/admin/users/${id}`, {
        method: "PATCH",
        body: JSON.stringify(patch),
      });
      await Promise.all([
        loadUsers(userPagination.page, userQuery),
        refreshAuditIfLoaded(),
      ]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "用户设置保存失败");
    } finally {
      pendingActions.current.delete(actionKey);
      setSaving(false);
    }
  }
  function confirmUserPatch(item: User, patch: Record<string, unknown>) {
    let consequence = "";
    if (patch.is_active === false) {
      consequence = `停用后，${item.real_name} 将立即退出所有设备且无法登录，但历史比赛会保留。`;
    } else if (patch.role === "system_admin") {
      consequence = `${item.real_name} 将可以管理用户、赛事、比赛结果和系统服务。`;
    } else if (patch.role === "user") {
      consequence = `${item.real_name} 将立即失去管理后台权限，并退出所有已登录设备。`;
    } else if (patch.is_test_account === true) {
      consequence = `${item.real_name} 参与或创建的相关房间会被标记为 QA 数据，并退出正式排行榜统计。`;
    }
    if (consequence && !window.confirm(`${consequence}\n\n确认继续？`)) return;
    void patchUser(item.id, patch);
  }
  function openPasswordReset(item: User) {
    setNotice("");
    setPasswordResetTarget(item);
    setPasswordResetForm({ new_password: "", confirm_password: "" });
  }
  async function resetPassword(event: React.FormEvent) {
    event.preventDefault();
    if (!passwordResetTarget) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      await apiFetch(
        `/api/admin/users/${passwordResetTarget.id}/reset-password`,
        { method: "POST", body: JSON.stringify(passwordResetForm) },
      );
      setNotice(
        `已重置 ${passwordResetTarget.real_name} 的密码，并退出其所有已登录设备。`,
      );
      setPasswordResetTarget(null);
      await Promise.all([
        loadUsers(userPagination.page, userQuery),
        refreshAuditIfLoaded(),
      ]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "密码重置失败");
    } finally {
      setSaving(false);
    }
  }
  async function patchCompetition(id: string, patch: Record<string, unknown>) {
    setSaving(true);
    setError("");
    try {
      await apiFetch(`/api/admin/competitions/${id}`, {
        method: "PATCH",
        body: JSON.stringify(patch),
      });
      await Promise.all([refreshCompetitionData(), refreshAuditIfLoaded()]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "赛事设置保存失败");
    } finally {
      setSaving(false);
    }
  }
  function toggleCompetition(item: Competition) {
    if (
      item.is_active &&
      !window.confirm(
        `停用“${competitionDisplayName(item)}”后，用户将不能再从赛事大厅创建新房间；已有房间和历史数据不受影响。\n\n确认继续？`,
      )
    ) return;
    void patchCompetition(item.id, { is_active: !item.is_active });
  }
  async function createSeason(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const result = await apiFetch<{ season: Season }>("/api/admin/seasons", {
        method: "POST",
        body: JSON.stringify({
          name: seasonForm.name,
          slug: seasonForm.slug,
          starts_at: new Date(seasonForm.starts_at).toISOString(),
          ends_at: seasonForm.ends_at ? new Date(seasonForm.ends_at).toISOString() : null,
          is_active: true,
        }),
      });
      setNotice(`已创建 ${result.season.name}，可将积分赛事切换到该赛季。`);
      setSeasonForm({ name: "", slug: "", starts_at: "", ends_at: "" });
      await Promise.all([refreshCompetitionData(), refreshAuditIfLoaded()]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "赛季创建失败");
    } finally {
      setSaving(false);
    }
  }
  async function patchSeason(id: string, patch: Record<string, unknown>) {
    setSaving(true);
    setError("");
    try {
      await apiFetch(`/api/admin/seasons/${id}`, {
        method: "PATCH",
        body: JSON.stringify(patch),
      });
      await Promise.all([refreshCompetitionData(), refreshAuditIfLoaded()]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "赛季设置保存失败");
    } finally {
      setSaving(false);
    }
  }
  function toggleSeason(item: Season) {
    if (
      item.is_active &&
      !window.confirm(
        `停用“${item.name}”后，绑定它的积分赛事将无法开始新比赛；系统会在仍有有效绑定时拒绝操作。\n\n确认继续？`,
      )
    ) return;
    void patchSeason(item.id, { is_active: !item.is_active });
  }
  async function patchTopic(
    competitionId: string,
    topicId: string,
    patch: Record<string, unknown>,
  ) {
    setSaving(true);
    setError("");
    try {
      await apiFetch(
        `/api/admin/competitions/${competitionId}/topics/${topicId}`,
        { method: "PATCH", body: JSON.stringify(patch) },
      );
      await Promise.all([refreshCompetitionData(), refreshAuditIfLoaded()]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "题目设置保存失败");
    } finally {
      setSaving(false);
    }
  }
  async function patchAgent(id: string, patch: Record<string, unknown>) {
    setSaving(true);
    setError("");
    try {
      await apiFetch(`/api/admin/agents/${id}`, {
        method: "PATCH",
        body: JSON.stringify(patch),
      });
      await Promise.all([refreshAgents(), refreshAuditIfLoaded()]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "AI 配置保存失败");
    } finally {
      setSaving(false);
    }
  }
  function editJudge(item?: JudgeProfile) {
    setNotice("");
    setJudgeTarget(item || null);
    setJudgeForm(
      item
        ? {
            name: item.name,
            endpoint: item.endpoint,
            model_name: item.model_name,
            system_prompt: item.system_prompt,
            timeout_seconds: item.timeout_seconds,
            is_active: item.is_active,
          }
        : {
            name: "",
            endpoint: "",
            model_name: "",
            system_prompt: "",
            timeout_seconds: 120,
            is_active: true,
          },
    );
  }
  async function submitJudge(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const result = await apiFetch<{ judge: JudgeProfile }>(
        judgeTarget
          ? `/api/admin/judges/${judgeTarget.id}`
          : "/api/admin/judges",
        {
          method: judgeTarget ? "PATCH" : "POST",
          body: JSON.stringify(judgeForm),
        },
      );
      editJudge();
      setNotice(
        `已保存裁判配置 ${result.judge.name}；新开始的比赛将固定使用该版本。`,
      );
      await Promise.all([
        refreshJudges(),
        refreshDashboard(),
        refreshAuditIfLoaded(),
      ]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "裁判配置保存失败");
    } finally {
      setSaving(false);
    }
  }
  async function activateJudge(item: JudgeProfile) {
    setSaving(true);
    setError("");
    try {
      await apiFetch(`/api/admin/judges/${item.id}`, {
        method: "PATCH",
        body: JSON.stringify({ is_active: true }),
      });
      setNotice(`已启用 ${item.name}，之后创建的比赛将使用该裁判。`);
      await Promise.all([
        refreshJudges(),
        refreshDashboard(),
        refreshAuditIfLoaded(),
      ]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "裁判切换失败");
    } finally {
      setSaving(false);
    }
  }
  async function saveProvider(kind: ProviderConfig["kind"]) {
    const item = providerDrafts[kind];
    if (!item) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const result = await apiFetch<{ provider: ProviderConfig }>(
        `/api/admin/providers/${kind}`,
        {
          method: "PUT",
          body: JSON.stringify({
            endpoint: item.endpoint,
            settings: item.settings,
            is_active: item.is_active,
          }),
        },
      );
      setNotice(`已保存 ${kind} 配置；只影响之后开始的比赛。`);
      setProviderDrafts((current) => ({ ...current, [kind]: result.provider }));
      await Promise.all([
        refreshProviders(),
        refreshDashboard(),
        refreshAuditIfLoaded(),
      ]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "语音服务配置保存失败");
    } finally {
      setSaving(false);
    }
  }
  async function createAudioCue(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setError("");
    setNotice("");
    const form = event.currentTarget;
    try {
      const result = await apiFetch<{ audio_cue: AudioCue }>(
        "/api/admin/audio-cues",
        { method: "POST", body: new FormData(form) },
      );
      setNotice(`已上传 ${result.audio_cue.name}，新比赛的同名阶段将优先复用该语音。`);
      form.reset();
      await Promise.all([refreshAudioCues(), refreshAuditIfLoaded()]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "预设语音上传失败");
    } finally {
      setSaving(false);
    }
  }
  async function patchAudioCue(id: string, patch: Record<string, unknown>) {
    setSaving(true);
    setError("");
    try {
      await apiFetch(`/api/admin/audio-cues/${id}`, {
        method: "PATCH",
        body: JSON.stringify(patch),
      });
      await Promise.all([refreshAudioCues(), refreshAuditIfLoaded()]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "预设语音设置保存失败");
    } finally {
      setSaving(false);
    }
  }
  function versionTemplate(item: AutomationTemplate) {
    setNotice("");
    setTemplateTarget(item);
    setTemplateName(item.name);
    setTemplateStages(JSON.stringify(item.stages, null, 2));
    setTemplateCompetitionIds(
      item.competitions.map((competition) => competition.id),
    );
  }
  async function submitTemplateVersion(event: React.FormEvent) {
    event.preventDefault();
    if (!templateTarget) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const stages = JSON.parse(templateStages);
      if (!Array.isArray(stages)) throw new Error("阶段配置必须是 JSON 数组。");
      const result = await apiFetch<{ template: AutomationTemplate }>(
        `/api/admin/automation-templates/${templateTarget.id}/versions`,
        {
          method: "POST",
          body: JSON.stringify({
            name: templateName.trim(),
            stages,
            competition_ids: templateCompetitionIds,
          }),
        },
      );
      setNotice(
        `已创建 ${result.template.name} v${result.template.version}，新房间将使用新版流程。`,
      );
      setTemplateTarget(null);
      await Promise.all([refreshAutomationData(), refreshAuditIfLoaded()]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "自动流程版本创建失败");
    } finally {
      setSaving(false);
    }
  }
  async function refreshMedia() {
    setSaving(true);
    setError("");
    try {
      setMedia(
        (await apiFetch<{ media: MediaStatus }>("/api/admin/media")).media,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "媒体盘点失败");
    } finally {
      setSaving(false);
    }
  }
  async function cleanupMedia() {
    if (!confirm("只会删除超过 24 小时且没有数据库引用的媒体文件。确定继续？"))
      return;
    setSaving(true);
    setError("");
    try {
      const result = await apiFetch<{ media: MediaStatus }>(
        "/api/admin/media/cleanup",
        {
          method: "POST",
          body: JSON.stringify({
            dry_run: false,
            min_age_hours: 24,
            include_stale_parts: true,
          }),
        },
      );
      setMedia(result.media);
      await refreshAuditIfLoaded();
    } catch (err) {
      setError(err instanceof Error ? err.message : "媒体清理失败");
    } finally {
      setSaving(false);
    }
  }
  async function refreshArchives() {
    setSaving(true);
    setError("");
    try {
      setArchives(
        (await apiFetch<{ archives: ArchiveStatus }>("/api/admin/archives"))
          .archives,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "比赛归档盘点失败");
    } finally {
      setSaving(false);
    }
  }
  async function refreshDataQuality() {
    setSaving(true);
    setError("");
    try {
      setDataQuality(await apiFetch<DataQualityStatus>("/api/admin/data-quality"));
    } catch (err) {
      setError(err instanceof Error ? err.message : "比赛数据质量检查失败");
    } finally {
      setSaving(false);
    }
  }
  async function updateDataIssue(
    speechId: string,
    issueCode: string,
    status: "needs_recollection" | "unrecoverable" | "verified",
    note: string,
  ) {
    setSaving(true);
    setError("");
    try {
      await apiFetch(`/api/admin/data-quality/speeches/${speechId}/issues/${issueCode}`, {
        method: "PATCH",
        body: JSON.stringify({ status, note }),
      });
      await refreshDataQuality();
      await refreshAuditIfLoaded();
      setNotice("数据异常处置记录已保存；原始比赛数据未被修改。");
    } catch (err) {
      setError(err instanceof Error ? err.message : "数据异常处置保存失败");
      throw err;
    } finally {
      setSaving(false);
    }
  }
  async function repairArchives() {
    if (!confirm("将重新生成所有缺失或校验失败的终局比赛归档。确定继续？"))
      return;
    setSaving(true);
    setError("");
    try {
      const result = await apiFetch<{
        archives: ArchiveStatus;
        repaired: string[];
        failed: string[];
      }>("/api/admin/archives/repair", { method: "POST", body: "{}" });
      setArchives(result.archives);
      await refreshAuditIfLoaded();
      setNotice(
        result.failed.length
          ? `已修复 ${result.repaired.length} 份归档，${result.failed.length} 份仍需处理。`
          : `已修复 ${result.repaired.length} 份比赛归档。`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "比赛归档修复失败");
    } finally {
      setSaving(false);
    }
  }
  async function cleanupArchives() {
    if (!confirm("只会删除超过 24 小时且数据库中已无对应比赛的归档文件。确定继续？"))
      return;
    setSaving(true);
    setError("");
    try {
      const result = await apiFetch<{ archives: ArchiveStatus }>(
        "/api/admin/archives/cleanup",
        {
          method: "POST",
          body: JSON.stringify({ dry_run: false, min_age_hours: 24 }),
        },
      );
      setArchives(result.archives);
      setNotice(`已清理 ${result.archives.deleted_files} 个孤儿归档文件。`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "比赛归档清理失败");
    } finally {
      setSaving(false);
    }
  }
  function addTopic(id: string) {
    setTopicCompetition(competitions.find((item) => item.id === id) || null);
    setTopicTitle("");
  }
  async function submitTopic(event: React.FormEvent) {
    event.preventDefault();
    if (!topicCompetition || topicTitle.trim().length < 4) return;
    setSaving(true);
    setError("");
    try {
      await apiFetch(`/api/admin/competitions/${topicCompetition.id}/topics`, {
        method: "POST",
        body: JSON.stringify({ title: topicTitle.trim() }),
      });
      setTopicCompetition(null);
      setTopicTitle("");
      await Promise.all([refreshCompetitionData(), refreshAuditIfLoaded()]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "添加题目失败");
    } finally {
      setSaving(false);
    }
  }
  function review(item: Review, mode: "approve" | "correct" = "approve") {
    setReviewMode(mode);
    setReviewTarget(item);
    setReviewForm({
      winner: item.winner || "aff",
      affirmative_score: item.affirmative_score || 85,
      negative_score: item.negative_score || 82,
      reasoning:
        item.reason ||
        (mode === "correct" ? "管理员修正赛果" : "管理员复核确认"),
    });
  }
  async function submitReview(event: React.FormEvent) {
    event.preventDefault();
    if (!reviewTarget) return;
    setSaving(true);
    setError("");
    try {
      await apiFetch(
        `/api/admin/reviews/${reviewTarget.scorecard_id}/${reviewMode === "correct" ? "correct" : "approve"}`,
        {
          method: "POST",
          body: JSON.stringify({
            ...reviewForm,
            expected_updated_at: reviewTarget.updated_at,
          }),
        },
      );
      setReviewTarget(null);
      await Promise.all([
        refreshReviewData(),
        refreshDashboard(),
        refreshAuditIfLoaded(),
      ]);
    } catch (err) {
      const message =
        err instanceof Error
          ? err.message
          : reviewMode === "correct"
            ? "赛果修正失败"
            : "复核提交失败";
      if (message.includes("其他管理员更新")) {
        setReviewTarget(null);
        setNotice("赛果已被其他管理员更新，列表已自动刷新。");
        await Promise.all([refreshReviewData(), refreshDashboard()]);
      } else {
        setError(message);
      }
    } finally {
      setSaving(false);
    }
  }
  async function retryJudge(item: Review) {
    setRetryingReviewIds((current) => {
      const next = new Set(current);
      next.add(item.scorecard_id);
      return next;
    });
    setError("");
    try {
      await apiFetch(`/api/admin/reviews/${item.scorecard_id}/retry`, {
        method: "POST",
        body: JSON.stringify({ expected_updated_at: item.updated_at }),
      });
      setNotice(`房间 #${item.room_code} 已使用当前启用裁判重新排队。`);
      await Promise.all([refreshReviewData(), refreshAuditIfLoaded()]);
    } catch (err) {
      const message = err instanceof Error ? err.message : "AI 裁判重试失败";
      if (message.includes("其他管理员更新")) {
        setNotice("该比赛已被其他管理员处理，列表已自动刷新。");
        await refreshReviewData();
      } else {
        setError(message);
      }
    } finally {
      setRetryingReviewIds((current) => {
        const next = new Set(current);
        next.delete(item.scorecard_id);
        return next;
      });
    }
  }
  function setTabLoading(tabId: AdminTab, value: boolean) {
    setLoadingTabs((current) => {
      const next = new Set(current);
      if (value) next.add(tabId);
      else next.delete(tabId);
      return next;
    });
  }
  async function loadTab(tabId: AdminTab, force = false) {
    if (!force && loadedTabs.current.has(tabId)) return;
    const pending = pendingTabLoads.current.get(tabId);
    if (pending) return pending;

    const request = (async () => {
      setTabLoading(tabId, true);
      setError("");
      try {
        switch (tabId) {
          case "overview":
            await refreshDashboard();
            break;
          case "rooms":
            await loadRooms(1, roomQuery, roomStatus, roomDataScope);
            break;
          case "users":
            await loadUsers(1, userQuery);
            break;
          case "competitions":
            await refreshCompetitionData();
            break;
          case "automation":
            await refreshAutomationData();
            break;
          case "agents":
            await Promise.all([
              refreshAgents(),
              refreshJudges(),
              refreshProviders(),
              refreshAudioCues(),
            ]);
            loadedTabs.current.add("agents");
            break;
          case "media": {
            const [mediaData, archiveData, qualityData] = await Promise.all([
              apiFetch<{ media: MediaStatus }>("/api/admin/media"),
              apiFetch<{ archives: ArchiveStatus }>("/api/admin/archives"),
              apiFetch<DataQualityStatus>("/api/admin/data-quality"),
            ]);
            setMedia(mediaData.media);
            setArchives(archiveData.archives);
            setDataQuality(qualityData);
            loadedTabs.current.add("media");
            break;
          }
          case "reviews":
            await refreshReviewData();
            break;
          case "audit":
            await loadAudit(1, auditQuery);
            break;
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "管理模块载入失败");
      } finally {
        pendingTabLoads.current.delete(tabId);
        setTabLoading(tabId, false);
      }
    })();
    pendingTabLoads.current.set(tabId, request);
    return request;
  }
  loadTabHandler.current = loadTab;
  useEffect(() => {
    if (!dash || restoredModule.current) return;
    restoredModule.current = true;
    const requested = adminTabFromLocation();
    if (requested === "overview") return;
    setTab(requested);
    void loadTabHandler.current?.(requested);
  }, [dash]);
  if (loading) return <PageLoading label="正在验证管理员身份…" detail="正在核对账号权限" />;
  if (!dash && error)
    return <LoadError message={error} retry={() => void load()} />;
  if (!dash) return <PageLoading label="正在载入管理系统…" detail="正在同步服务状态与比赛数据" />;
  const activateTab = (nextTab: AdminTab) => {
    setTab(nextTab);
    persistAdminTab(nextTab);
    void loadTab(nextTab);
  };
  const moveTabFocus = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    currentTab: AdminTab,
  ) => {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const currentIndex = tabs.findIndex((item) => item.id === currentTab);
    const nextIndex = event.key === "Home"
      ? 0
      : event.key === "End"
        ? tabs.length - 1
        : (currentIndex + (event.key === "ArrowDown" ? 1 : -1) + tabs.length) % tabs.length;
    const nextTab = tabs[nextIndex].id;
    activateTab(nextTab);
    requestAnimationFrame(() =>
      document.getElementById(`admin-tab-${nextTab}`)?.focus(),
    );
  };
  return (
    <div className="page-shell">
      <div className="section-head">
        <div>
          <span className="eyebrow">系统管理</span>
          <h1>稷下辩论系统管理</h1>
          <p>全局设置、赛事配置、服务状态和审计中心，仅系统管理员可见。</p>
        </div>
        <div className={`badge ${dash.system_health.ok ? "live" : ""}`}>
          <Activity size={13} />
          {dash.system_health.ok ? "平台已就绪" : "平台需要检查"}
        </div>
      </div>
      {error && <div className="error-box" role="alert">{error}</div>}
      {notice && (
        <div className="notice-box" role="status">
          {notice}
        </div>
      )}
      <div className="admin-layout">
        <label className="panel admin-mobile-picker">
          <span>管理模块</span>
          <select
            className="select"
            aria-label="管理模块"
            value={tab}
            onChange={(event) => activateTab(event.target.value as AdminTab)}
          >
            {tabs.map((item) => <option value={item.id} key={item.id}>{item.label}</option>)}
          </select>
        </label>
        <aside className="panel admin-nav" aria-label="管理模块导航">
          {tabs.map((item) => (
            <button
              id={`admin-tab-${item.id}`}
              className={tab === item.id ? "active" : ""}
              aria-current={tab === item.id ? "page" : undefined}
              aria-controls="admin-module-content"
              tabIndex={tab === item.id ? 0 : -1}
              onClick={() => activateTab(item.id)}
              onKeyDown={(event) => moveTabFocus(event, item.id)}
              key={item.id}
            >
              <item.icon size={16} /> {item.label}
            </button>
          ))}
        </aside>
        <section id="admin-module-content" className="panel admin-content" aria-busy={loadingTabs.has(tab)} aria-labelledby={`admin-tab-${tab}`}>
          <div className="admin-module-toolbar">
            <span className="muted" role="status" aria-live="polite">
              {loadingTabs.has(tab)
                ? `正在载入${tabs.find((item) => item.id === tab)?.label}…`
                : loadedTabs.current.has(tab)
                  ? "数据已载入"
                  : "等待载入"}
            </span>
            <button
              className="button button-small button-secondary"
              disabled={loadingTabs.has(tab) || saving}
              onClick={() => void loadTab(tab, true)}
            >
              <RefreshCw size={15} />
              {loadingTabs.has(tab) ? "刷新中…" : "刷新当前模块"}
            </button>
          </div>
          {tab === "overview" && (
            <SystemOverview dashboard={dash} />
          )}
          {tab === "rooms" && (
            <RoomsPanel
              rooms={rooms}
              query={roomQuery}
              status={roomStatus}
              dataScope={roomDataScope}
              pagination={roomPagination}
              loading={roomsLoading}
              saving={saving}
              onQueryChange={setRoomQuery}
              onStatusChange={setRoomStatus}
              onDataScopeChange={setRoomDataScope}
              onLoad={(...args) => void loadRooms(...args)}
              onMarkAsTestData={(room) => void markRoomAsTestData(room)}
            />
          )}
          {tab === "users" && (
            <UsersPanel
              currentUserId={user?.id}
              users={users}
              query={userQuery}
              pagination={userPagination}
              loading={usersLoading}
              saving={saving}
              onQueryChange={setUserQuery}
              onSearch={(...args) => void loadUsers(...args)}
              onPasswordReset={openPasswordReset}
              onPatch={confirmUserPatch}
            />
          )}
          {tab === "competitions" && (
            <CompetitionsModule
              competitions={competitions}
              seasons={seasons}
              seasonForm={seasonForm}
              saving={saving}
              onSeasonFormChange={setSeasonForm}
              onCreateSeason={createSeason}
              onToggleSeason={toggleSeason}
              onPatchCompetition={(id, patch) => void patchCompetition(id, patch)}
              onPatchTopic={(competitionId, topicId, patch) => void patchTopic(competitionId, topicId, patch)}
              onAddTopic={addTopic}
              onToggleCompetition={toggleCompetition}
            />
          )}
          {tab === "automation" && (
            <AutomationModule
              templates={automationTemplates}
              saving={saving}
              onVersion={versionTemplate}
            />
          )}
          {tab === "agents" && (
            <AgentsModule
              dashboard={dash}
              agents={agents}
              audioCues={audioCues}
              judges={judges}
              providerConfigs={providerConfigs}
              providerDrafts={providerDrafts}
              judgeTarget={judgeTarget}
              judgeForm={judgeForm}
              saving={saving}
              onProviderDraftsChange={setProviderDrafts}
              onJudgeFormChange={setJudgeForm}
              onSaveProvider={(kind) => void saveProvider(kind)}
              onEditJudge={editJudge}
              onSubmitJudge={submitJudge}
              onActivateJudge={(judge) => void activateJudge(judge)}
              onPatchAgent={(id, patch) => void patchAgent(id, patch)}
              onCreateAudioCue={createAudioCue}
              onPatchAudioCue={(id, patch) => void patchAudioCue(id, patch)}
            />
          )}
          {tab === "media" && (
            <MediaModule
              media={media}
              archives={archives}
              dataQuality={dataQuality}
              saving={saving}
              onRefreshMedia={() => void refreshMedia()}
              onCleanupMedia={() => void cleanupMedia()}
              onRefreshArchives={() => void refreshArchives()}
              onRefreshDataQuality={() => void refreshDataQuality()}
              onUpdateDataIssue={updateDataIssue}
              onRepairArchives={() => void repairArchives()}
              onCleanupArchives={() => void cleanupArchives()}
            />
          )}
          {tab === "reviews" && (
            <ReviewsModule
              reviews={reviews}
              recentReviews={recentReviews}
              speechCorrections={speechCorrections}
              recentSpeechCorrections={recentSpeechCorrections}
              retryingIds={retryingReviewIds}
              saving={saving}
              onRetry={(item) => void retryJudge(item)}
              onReview={review}
              onSpeechCorrectionReview={reviewSpeechCorrection}
            />
          )}
          {tab === "audit" && (
            <AuditModule
              items={audit}
              query={auditQuery}
              pagination={auditPagination}
              loading={auditLoading}
              onQueryChange={setAuditQuery}
              onLoad={(page, query) => void loadAudit(page, query)}
            />
          )}
        </section>
      </div>
      {topicCompetition && (
        <TopicDialog
          competition={topicCompetition}
          title={topicTitle}
          saving={saving}
          onTitleChange={setTopicTitle}
          onClose={() => setTopicCompetition(null)}
          onSubmit={submitTopic}
        />
      )}
      {passwordResetTarget && (
        <PasswordResetDialog
          user={passwordResetTarget}
          form={passwordResetForm}
          saving={saving}
          onFormChange={setPasswordResetForm}
          onClose={() => setPasswordResetTarget(null)}
          onSubmit={resetPassword}
        />
      )}
      {templateTarget && (
        <TemplateVersionDialog
          template={templateTarget}
          competitions={competitions}
          name={templateName}
          stages={templateStages}
          competitionIds={templateCompetitionIds}
          saving={saving}
          onNameChange={setTemplateName}
          onStagesChange={setTemplateStages}
          onCompetitionIdsChange={setTemplateCompetitionIds}
          onClose={() => setTemplateTarget(null)}
          onSubmit={submitTemplateVersion}
        />
      )}
      {reviewTarget && (
        <ReviewDialog
          review={reviewTarget}
          mode={reviewMode}
          form={reviewForm}
          saving={saving}
          onFormChange={setReviewForm}
          onClose={() => setReviewTarget(null)}
          onSubmit={submitReview}
        />
      )}
    </div>
  );
}
