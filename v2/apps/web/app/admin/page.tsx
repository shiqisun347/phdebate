"use client";

import Link from "next/link";
import {
  Activity,
  Bot,
  FileCheck2,
  Gauge,
  HardDrive,
  Library,
  Radio,
  RefreshCw,
  Scale,
  Shield,
  Trash2,
  Users,
  Volume2,
  Workflow,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "@/lib/api";
import { competitionDisplayName, competitionDisplayNameFromStoredName } from "@/lib/primary-competition";
import { providerStatusLabel } from "@/lib/status-labels";
import { useSession } from "@/lib/use-session";
import type { Competition, Room, Season, User } from "@/lib/types";
import { LoadError } from "@/components/load-error";
import { AdminPagination, type AdminPaginationState } from "@/components/admin/admin-pagination";
import { RoomsPanel } from "@/components/admin/rooms-panel";
import {
  healthDetail,
  providerDisplayLabels,
  SystemOverview,
  type AdminDashboard,
} from "@/components/admin/system-overview";
import { UsersPanel } from "@/components/admin/users-panel";

type Review = {
  scorecard_id: string;
  match_id: string;
  room_code: string;
  topic: string;
  status: "review_required" | "approved";
  winner: "aff" | "neg" | "draw" | null;
  affirmative_score: number;
  negative_score: number;
  reason: string;
  updated_at: string;
};
type Audit = {
  id: string;
  actor_name: string;
  action: string;
  target_type: string;
  target_id: string;
  payload: Record<string, unknown>;
  created_at: string;
};
type MediaStatus = {
  scanned_files: number;
  scanned_bytes: number;
  referenced_files: number;
  referenced_existing_files: number;
  missing_reference_count: number;
  missing_references: string[];
  orphan_candidate_count: number;
  orphan_candidate_bytes: number;
  orphan_candidates: { path: string; bytes: number; modified_at: string }[];
  unsafe_entries: number;
  truncated: boolean;
  disk_total_bytes: number;
  disk_used_bytes: number;
  disk_free_bytes: number;
  deleted_files: number;
  deleted_bytes: number;
};
type ArchiveStatus = {
  scanned_files: number;
  scanned_bytes: number;
  expected_matches: number;
  complete_archives: number;
  invalid_archive_count: number;
  invalid_archives: { match_id: string; reason: string }[];
  orphan_candidate_count: number;
  orphan_candidate_bytes: number;
  orphan_candidates: { path: string; bytes: number; modified_at: string }[];
  unsafe_entries: number;
  unmanaged_entries: number;
  truncated: boolean;
  deleted_files: number;
  deleted_bytes: number;
};
type AutomationTemplate = {
  id: string;
  slug: string;
  name: string;
  version: number;
  stages: Record<string, unknown>[];
  is_active: boolean;
  competitions: { id: string; name: string }[];
  created_at: string;
};
type AudioCue = {
  id: string;
  key: string;
  name: string;
  text: string;
  audio_url: string;
  is_active: boolean;
  updated_at: string;
};
type JudgeProfile = {
  id: string;
  name: string;
  endpoint: string;
  model_name: string;
  system_prompt: string;
  timeout_seconds: number;
  is_active: boolean;
  created_at: string;
  updated_at: string;
};
type ProviderConfig = {
  id: string;
  kind: "agent" | "funasr" | "lighttts";
  endpoint: string;
  settings: Record<string, number>;
  has_secret?: boolean;
  is_active: boolean;
  created_at: string;
  updated_at: string;
};
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
const bytes = (value: number) =>
  value < 1024
    ? `${value} B`
    : value < 1024 ** 2
      ? `${(value / 1024).toFixed(1)} KiB`
      : value < 1024 ** 3
        ? `${(value / 1024 ** 2).toFixed(1)} MiB`
        : `${(value / 1024 ** 3).toFixed(2)} GiB`;
const auditActionLabels: Record<string, string> = {
  "archives.repair": "修复比赛归档",
  "automation_template.version_create": "创建自动流程版本",
  "judge.correct": "修正比赛结果",
  "judge.retry": "重试 AI 裁判",
  "judge_profile.create": "创建裁判配置",
  "judge_profile.patch": "修改裁判配置",
  "match.judge_snapshot.backfill": "补齐裁判配置快照",
  "provider_config.patch": "修改服务配置",
  "provider_config.test": "测试服务连接",
  "qa_provisioning.apply": "执行 QA 验收配置",
  "room.data_scope.patch": "修改比赛数据类型",
  "seat.restore_human": "恢复真人席位",
  "topic.create": "添加赛事题目",
  "topic.patch": "修改赛事题目",
  "user.patch": "修改用户",
  "user.password_reset": "重置用户密码",
};
const auditTargetLabels: Record<string, string> = {
  archive_storage: "比赛归档",
  automation_template: "自动流程",
  judge_profile: "裁判配置",
  match: "比赛",
  provider_config: "服务配置",
  qa_provisioning: "QA 验收",
  room: "房间",
  room_seat: "比赛席位",
  scorecard: "裁判结果",
  topic: "赛事题目",
  user: "用户",
};
const isLegacyCompetition = (competition: Pick<Competition, "slug" | "format">) =>
  competition.format === "legacy" || competition.slug.startsWith("legacy");
const emptyPagination: AdminPaginationState = {
  page: 1,
  page_size: 100,
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
  const [rooms, setRooms] = useState<Room[]>([]);
  const [roomQuery, setRoomQuery] = useState("");
  const [roomStatus, setRoomStatus] = useState("");
  const [roomDataScope, setRoomDataScope] = useState("");
  const [roomPagination, setRoomPagination] =
    useState<AdminPaginationState>(emptyPagination);
  const [agents, setAgents] = useState<
    {
      id: string;
      name: string;
      profile_key: string;
      provider: string;
      voice_id: string;
      is_active: boolean;
    }[]
  >([]);
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
  const [reviews, setReviews] = useState<Review[]>([]);
  const [recentReviews, setRecentReviews] = useState<Review[]>([]);
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
    if (!loading && user?.role !== "system_admin") router.replace("/");
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
    const data = await apiFetch<{ items: Review[]; recent: Review[] }>(
      "/api/admin/reviews",
    );
    setReviews(data.items);
    setRecentReviews(data.recent || []);
    loadedTabs.current.add("reviews");
  }
  useEffect(() => {
    if (user?.role === "system_admin") void load();
  }, [load, user?.role]);
  async function loadUsers(page = 1, q = userQuery) {
    const requestSequence = ++usersRequestSequence.current;
    setUsersLoading(true);
    try {
      const data = await apiFetch<{ items: User[]; pagination: AdminPaginationState }>(
        `/api/admin/users?page=${page}&page_size=100&q=${encodeURIComponent(q)}`,
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
      const data = await apiFetch<{ items: Room[]; pagination: AdminPaginationState }>(
        `/api/admin/rooms?page=${page}&page_size=100&q=${encodeURIComponent(q)}&status=${encodeURIComponent(status)}&data_scope=${encodeURIComponent(dataScope)}`,
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
        `/api/admin/audit?page=${page}&page_size=100&q=${encodeURIComponent(q)}`,
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
  async function markRoomAsTestData(room: Room) {
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
            const [mediaData, archiveData] = await Promise.all([
              apiFetch<{ media: MediaStatus }>("/api/admin/media"),
              apiFetch<{ archives: ArchiveStatus }>("/api/admin/archives"),
            ]);
            setMedia(mediaData.media);
            setArchives(archiveData.archives);
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
  if (loading) return <div className="loading-screen">正在验证管理员身份…</div>;
  if (!dash && error)
    return <LoadError message={error} retry={() => void load()} />;
  if (!dash) return <div className="loading-screen">正在载入管理系统…</div>;
  const visibleProviders = Object.entries(dash.providers).filter(
    ([key, value]) => key !== "lighttts" || value.enabled,
  );
  const realtimeTtsHealth = dash.system_health.checks.moss_tts_realtime;
  const activateTab = (nextTab: AdminTab) => {
    setTab(nextTab);
    void loadTab(nextTab);
  };
  return (
    <div className="page-shell">
      <div className="section-head">
        <div>
          <span className="eyebrow">System Administration</span>
          <h2>稷下辩论系统管理</h2>
          <p>全局设置、赛事配置、服务状态和审计中心，仅系统管理员可见。</p>
        </div>
        <div className={`badge ${dash.system_health.ok ? "live" : ""}`}>
          <Activity size={13} />
          {dash.system_health.ok ? "V2 已就绪" : "V2 需要检查"}
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
              onClick={() => activateTab(item.id)}
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
            <>
              <div className="panel-title">
                <h2>赛事与题库</h2>
                <span className="muted">
                  题目停用后不会出现在新建房间中，历史比赛不受影响
                </span>
              </div>
              <div className="panel-title" style={{ marginTop: 24 }}>
                <h3>赛季生命周期</h3>
                <span className="badge">房间创建时固定赛季</span>
              </div>
              <form className="panel-subsection form-stack" onSubmit={createSeason}>
                <div className="form-grid">
                  <div className="field"><label htmlFor="season-name">赛季名称</label><input id="season-name" className="input" required maxLength={100} value={seasonForm.name} onChange={(event) => setSeasonForm({ ...seasonForm, name: event.target.value })} placeholder="第二赛季" /></div>
                  <div className="field"><label htmlFor="season-slug">赛季标识</label><input id="season-slug" className="input" required pattern="[a-z0-9][a-z0-9-]*" maxLength={80} value={seasonForm.slug} onChange={(event) => setSeasonForm({ ...seasonForm, slug: event.target.value.toLowerCase() })} placeholder="season-2" /></div>
                </div>
                <div className="form-grid">
                  <div className="field"><label htmlFor="season-start">开始时间</label><input id="season-start" className="input" type="datetime-local" required value={seasonForm.starts_at} onChange={(event) => setSeasonForm({ ...seasonForm, starts_at: event.target.value })} /></div>
                  <div className="field"><label htmlFor="season-end">结束时间</label><input id="season-end" className="input" type="datetime-local" value={seasonForm.ends_at} onChange={(event) => setSeasonForm({ ...seasonForm, ends_at: event.target.value })} /></div>
                </div>
                <div className="card-actions"><button className="button button-small" disabled={saving}>创建赛季</button></div>
              </form>
              <div className="table-wrap" role="region" aria-label="赛季管理表格" tabIndex={0} style={{ marginTop: 18 }}>
                <table>
                  <thead><tr><th>赛季</th><th>时间范围</th><th>赛事 / 比赛</th><th>状态</th><th>操作</th></tr></thead>
                  <tbody>
                    {seasons.map((season) => (
                      <tr key={season.id}>
                        <td><strong>{season.name}</strong><small className="muted">{season.slug}</small></td>
                        <td>{new Date(season.starts_at).toLocaleString("zh-CN")}<small className="muted">至 {season.ends_at ? new Date(season.ends_at).toLocaleString("zh-CN") : "长期"}</small></td>
                        <td>{season.competition_count || 0} 个赛事 · {season.match_count || 0} 场比赛</td>
                        <td>{season.is_open ? "进行中" : season.is_active ? "未开放或已结束" : "已停用"}</td>
                        <td><button className="text-button" disabled={saving} onClick={() => toggleSeason(season)}>{season.is_active ? "停用" : "启用"}</button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="panel-title" style={{ marginTop: 30 }}><h3>赛事配置与题库</h3></div>
              <div className="competition-grid">
                {competitions.map((item) => (
                  <article className="competition-card" key={item.id}>
                    <span className="badge">{item.format}</span>
                    <h3>{competitionDisplayName(item)}</h3>
                    <p>{item.tagline}</p>
                    <div className="card-meta">
                      <span>{item.ranked ? "积分赛" : "训练赛"}</span>
                      <span>{item.seat_count} 席位</span>
                    </div>
                    {isLegacyCompetition(item) ? (
                      <div className="field">
                        <span className="label">归档属性</span>
                        <strong>历史记录 · 永久只读</strong>
                        <small className="muted">仅用于查询迁移比赛，不能重新启用、绑定赛季或维护题库。</small>
                      </div>
                    ) : (
                      <div className="field">
                        <label htmlFor={`competition-season-${item.id}`}>绑定赛季</label>
                        <select id={`competition-season-${item.id}`} className="select" value={item.season?.id || ""} disabled={saving} onChange={(event) => void patchCompetition(item.id, { season_id: event.target.value })}>
                          <option value="" disabled>未绑定赛季</option>
                          {seasons.filter((season) => season.is_active).map((season) => <option key={season.id} value={season.id}>{season.name}{season.is_open ? " · 进行中" : ""}</option>)}
                        </select>
                        <small className="muted">已创建房间继续使用原赛季</small>
                      </div>
                    )}
                    <details className="competition-topics">
                      <summary>
                        题库管理 ·{" "}
                        {item.topics?.filter(
                          (topic) => topic.is_active !== false,
                        ).length || 0}{" "}
                        个启用 / {item.topics?.length || 0} 个全部
                      </summary>
                      <div className="history-list">
                        {item.topics?.map((topic) => (
                          <div
                            className="history-row"
                            style={{ gridTemplateColumns: "1fr auto" }}
                            key={topic.id}
                          >
                            <span className="row-main">
                              <strong>{topic.title}</strong>
                              <small>
                                {topic.is_active === false
                                  ? "已停用"
                                  : "创建房间时可选"}
                              </small>
                            </span>
                            {!isLegacyCompetition(item) && (
                              <button
                                className="text-button"
                                disabled={saving}
                                onClick={() =>
                                  void patchTopic(item.id, topic.id, {
                                    is_active: topic.is_active === false,
                                  })
                                }
                              >
                                {topic.is_active === false ? "启用" : "停用"}
                              </button>
                            )}
                          </div>
                        ))}
                        {!item.topics?.length && (
                          <div className="empty">该赛事还没有题目</div>
                        )}
                      </div>
                    </details>
                    {!isLegacyCompetition(item) && (
                      <div className="card-actions">
                        <button
                          className="button button-small"
                          onClick={() => addTopic(item.id)}
                        >
                          添加题目
                        </button>
                        <button
                          className="button button-small button-secondary"
                          onClick={() => toggleCompetition(item)}
                        >
                          {item.is_active === false ? "启用赛事" : "停用赛事"}
                        </button>
                      </div>
                    )}
                  </article>
                ))}
              </div>
            </>
          )}
          {tab === "automation" && (
            <>
              <div className="panel-title">
                <h2>自动比赛流程</h2>
                <span className="muted">
                  每次修改创建新版本；已创建房间继续使用原快照
                </span>
              </div>
              <div className="table-wrap" role="region" aria-label="自动流程模板表格" tabIndex={0}>
                <table>
                  <thead>
                    <tr>
                      <th>模板</th>
                      <th>版本</th>
                      <th>阶段</th>
                      <th>绑定赛事</th>
                      <th>创建时间</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {automationTemplates.map((item) => (
                      <tr key={item.id}>
                        <td>
                          <strong>{item.name}</strong>
                          <small className="muted"> · {item.slug}</small>
                        </td>
                        <td>v{item.version}</td>
                        <td>{item.stages.length}</td>
                        <td>
                          {item.competitions
                            .map((competition) => competitionDisplayNameFromStoredName(competition.name))
                            .join("、") || "历史版本"}
                        </td>
                        <td>
                          {new Date(item.created_at).toLocaleString("zh-CN")}
                        </td>
                        <td>
                          {item.slug.startsWith("legacy") ? (
                            <span className="muted">只读归档</span>
                          ) : (
                            <button
                              className="text-button"
                              disabled={saving}
                              onClick={() => versionTemplate(item)}
                            >
                              创建新版本
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {!automationTemplates.length && (
                  <div className="empty">暂无自动流程模板</div>
                )}
              </div>
            </>
          )}
          {tab === "agents" && (
            <>
              <div className="panel-title">
                <h2>AI 辩手与语音服务</h2>
                <div className="card-actions">
                  <Link className="button button-small" href="/admin/agent-access">RESTful Agent 接入</Link>
                  <span className="badge">玩家不可见</span>
                </div>
              </div>
              <div className="service-grid">
                {visibleProviders.map(([key, value]) => (
                  <div className="service-card" key={key}>
                    <i
                      className={`status-dot ${value.healthy === true ? "online" : value.enabled ? "warning" : ""}`}
                    />
                    <div>
                      <strong>
                        {providerDisplayLabels[key] || key} · {providerStatusLabel[value.status] || value.status}
                      </strong>
                      <small>
                        {value.endpoint || "未配置"}
                        {value.message ? ` · ${value.message}` : ""}
                      </small>
                    </div>
                  </div>
                ))}
              </div>
              <div className="panel-title" style={{ marginTop: 30 }}>
                <h3><Activity size={18} />语音服务运行配置</h3>
                <span className="badge">开赛时固定</span>
              </div>
              <p className="muted">
                修改只影响之后开始的比赛；进行中的房间继续使用自己的语音配置快照。当前 MOSS 实时语音链路由服务器统一托管，此处只读，避免误改可靠播放基线。
              </p>
              <div className="provider-config-grid">
                {realtimeTtsHealth && (
                  <section className="panel-subsection form-stack" aria-label="MOSS 实时语音合成运行状态">
                    <div className="panel-title">
                      <h3>MOSS 实时语音合成</h3>
                      <span className={`badge ${realtimeTtsHealth.ok ? "live" : "neg-badge"}`}>{realtimeTtsHealth.ok ? "当前运行" : "需要检查"}</span>
                    </div>
                    <div className="service-card">
                      <i className={`status-dot ${realtimeTtsHealth.ok ? "online" : "warning"}`} />
                      <div>
                        <strong>{realtimeTtsHealth.ok ? "实时语音链路正常" : "实时语音链路异常"}</strong>
                        <small>{healthDetail(realtimeTtsHealth)}</small>
                      </div>
                    </div>
                    <p className="muted">模型、并发、流式会话和浏览器播放参数由服务器部署配置管理；本页面不会修改这些参数。</p>
                  </section>
                )}
                {providerConfigs.filter((item) => item.kind !== "agent" && (item.kind !== "lighttts" || item.is_active)).map((item) => {
                  const draft = providerDrafts[item.kind] || item;
                  return (
                    <form className="panel-subsection form-stack" key={item.kind} onSubmit={(event) => { event.preventDefault(); void saveProvider(item.kind); }}>
                      <div className="panel-title">
                        <h3>{item.kind === "funasr" ? "FunASR 语音识别" : "兼容 LightTTS 语音合成"}</h3>
                        <span className={`badge ${draft.is_active ? "" : "neg-badge"}`}>{draft.is_active ? "启用" : "停用"}</span>
                      </div>
                      <div className="field">
                        <label htmlFor={`provider-${item.kind}-endpoint`}>服务地址</label>
                        <input id={`provider-${item.kind}-endpoint`} className="input" required value={draft.endpoint} onChange={(event) => setProviderDrafts((current) => ({ ...current, [item.kind]: { ...draft, endpoint: event.target.value } }))} />
                      </div>
                      {item.kind === "funasr" ? (
                        <div className="field">
                          <label htmlFor="provider-funasr-final-wait">离线最终结果等待秒数</label>
                          <input id="provider-funasr-final-wait" className="input" type="number" min={5} max={60} step={0.5} value={draft.settings.final_wait_seconds ?? 30} onChange={(event) => setProviderDrafts((current) => ({ ...current, [item.kind]: { ...draft, settings: { ...draft.settings, final_wait_seconds: Number(event.target.value) } } }))} />
                        </div>
                      ) : (
                        <div className="form-grid">
                          <div className="field">
                            <label htmlFor="provider-lighttts-timeout">读取超时秒数</label>
                            <input id="provider-lighttts-timeout" className="input" type="number" min={30} max={300} value={draft.settings.read_timeout_seconds ?? 180} onChange={(event) => setProviderDrafts((current) => ({ ...current, [item.kind]: { ...draft, settings: { ...draft.settings, read_timeout_seconds: Number(event.target.value) } } }))} />
                          </div>
                          <div className="field">
                            <label htmlFor="provider-lighttts-speed">语速</label>
                            <input id="provider-lighttts-speed" className="input" type="number" min={0.5} max={2} step={0.05} value={draft.settings.speed ?? 1} onChange={(event) => setProviderDrafts((current) => ({ ...current, [item.kind]: { ...draft, settings: { ...draft.settings, speed: Number(event.target.value) } } }))} />
                          </div>
                        </div>
                      )}
                      <label className="check-row" htmlFor={`provider-${item.kind}-active`}>
                        <input id={`provider-${item.kind}-active`} type="checkbox" checked={draft.is_active} onChange={(event) => setProviderDrafts((current) => ({ ...current, [item.kind]: { ...draft, is_active: event.target.checked } }))} />
                        新比赛启用该服务
                      </label>
                      <div className="card-actions"><button className="button button-small" disabled={saving}>保存 {item.kind}</button></div>
                    </form>
                  );
                })}
              </div>
              <div className="panel-title" style={{ marginTop: 30 }}>
                <h3><Scale size={18} />AI 裁判配置</h3>
                <button className="button button-small button-secondary" disabled={saving} onClick={() => editJudge()}>
                  新建裁判
                </button>
              </div>
              <p className="muted">
                开赛时会把当前启用配置固定到比赛快照；之后切换裁判不会改变进行中或历史比赛。非法胜方、NaN、越界分数和空判定理由会自动进入人工复核。
              </p>
              <form className="form-stack panel-subsection" onSubmit={submitJudge}>
                <div className="form-grid">
                  <div className="field">
                    <label htmlFor="judge-name">配置名称</label>
                    <input id="judge-name" className="input" required maxLength={100} value={judgeForm.name} onChange={(event) => setJudgeForm({ ...judgeForm, name: event.target.value })} placeholder="正式赛 AI 裁判" />
                  </div>
                  <div className="field">
                    <label htmlFor="judge-model">模型名称</label>
                    <input id="judge-model" className="input" required maxLength={120} value={judgeForm.model_name} onChange={(event) => setJudgeForm({ ...judgeForm, model_name: event.target.value })} placeholder="judge-model" />
                  </div>
                </div>
                <div className="field">
                  <label htmlFor="judge-endpoint">POST 服务地址</label>
                  <input id="judge-endpoint" className="input" type="url" required maxLength={500} value={judgeForm.endpoint} onChange={(event) => setJudgeForm({ ...judgeForm, endpoint: event.target.value })} placeholder="https://example.com/api/judge" />
                </div>
                <div className="field">
                  <label htmlFor="judge-prompt">裁判指令</label>
                  <textarea id="judge-prompt" className="textarea" maxLength={10000} value={judgeForm.system_prompt} onChange={(event) => setJudgeForm({ ...judgeForm, system_prompt: event.target.value })} placeholder="说明评分维度、胜负标准和输出格式" />
                </div>
                <div className="form-grid">
                  <div className="field">
                    <label htmlFor="judge-timeout">超时秒数</label>
                    <input id="judge-timeout" className="input" type="number" min={10} max={300} required value={judgeForm.timeout_seconds} onChange={(event) => setJudgeForm({ ...judgeForm, timeout_seconds: Number(event.target.value) })} />
                  </div>
                  <label className="check-row" htmlFor="judge-active">
                    <input id="judge-active" type="checkbox" checked={judgeForm.is_active} onChange={(event) => setJudgeForm({ ...judgeForm, is_active: event.target.checked })} />
                    保存后设为当前启用裁判
                  </label>
                </div>
                <div className="card-actions">
                  <button className="button button-small" disabled={saving}>{saving ? "保存中…" : judgeTarget ? "保存裁判修改" : "创建裁判配置"}</button>
                  {judgeTarget && <button type="button" className="button button-small button-secondary" onClick={() => editJudge()} disabled={saving}>取消编辑</button>}
                </div>
              </form>
              <div className="table-wrap" role="region" aria-label="AI 裁判配置表格" tabIndex={0} style={{ marginTop: 18 }}>
                <table>
                  <thead><tr><th>名称</th><th>模型与地址</th><th>超时</th><th>状态</th><th>操作</th></tr></thead>
                  <tbody>
                    {judges.map((item) => (
                      <tr key={item.id}>
                        <td><strong>{item.name}</strong></td>
                        <td><strong>{item.model_name}</strong><small className="muted">{item.endpoint}</small></td>
                        <td>{item.timeout_seconds} 秒</td>
                        <td>{item.is_active ? "当前启用" : "备用"}</td>
                        <td>
                          <button className="text-button" disabled={saving} onClick={() => editJudge(item)}>编辑</button>
                          {!item.is_active && <button className="text-button" disabled={saving} onClick={() => void activateJudge(item)}>启用</button>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {!judges.length && <div className="empty">尚未配置 AI 裁判；比赛结束时会进入管理员复核。</div>}
              </div>
              <h3 style={{ marginTop: 26 }}>辩手席位池</h3>
              <div className="table-wrap" role="region" aria-label="辩手席位池表格" tabIndex={0}>
                <table>
                  <thead>
                    <tr>
                      <th>名称</th>
                      <th>远程人设</th>
                      <th>音色</th>
                      <th>状态</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {agents.map((item) => (
                      <tr key={item.id}>
                        <td>{item.name}</td>
                        <td><code>{item.profile_key}</code></td>
                        <td>{item.voice_id}</td>
                        <td>{item.is_active ? "启用" : "停用"}</td>
                        <td>
                          <button
                            className="text-button"
                            disabled={saving}
                            onClick={() =>
                              void patchAgent(item.id, {
                                is_active: !item.is_active,
                              })
                            }
                          >
                            {item.is_active ? "停用" : "启用"}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="panel-title" style={{ marginTop: 30 }}>
                <h3><Volume2 size={18} />阶段预设语音</h3>
                <span className="badge">{audioCues.filter((item) => item.is_active).length} 条启用</span>
              </div>
              <p className="muted">
                key 必须与自动流程阶段 key 完全一致。新比赛准备阶段会优先复制预设 WAV；未配置、停用或文件缺失时自动回退 MOSS 实时 TTS。
              </p>
              <form className="form-stack panel-subsection" onSubmit={createAudioCue}>
                <div className="form-grid">
                  <div className="field">
                    <label htmlFor="audio-cue-key">阶段 key</label>
                    <input id="audio-cue-key" name="key" className="input" required minLength={2} maxLength={80} pattern="[a-z][a-z0-9_]{1,79}" placeholder="opening" />
                  </div>
                  <div className="field">
                    <label htmlFor="audio-cue-name">显示名称</label>
                    <input id="audio-cue-name" name="name" className="input" required maxLength={120} placeholder="开场规则播报" />
                  </div>
                </div>
                <div className="field">
                  <label htmlFor="audio-cue-text">对应播报文本</label>
                  <textarea id="audio-cue-text" name="text" className="textarea" maxLength={2000} placeholder="用于核对预设音频内容；实际播放上传的 WAV。" />
                </div>
                <div className="field">
                  <label htmlFor="audio-cue-file">PCM WAV 文件</label>
                  <input id="audio-cue-file" name="audio" className="input" type="file" accept="audio/wav,.wav" required />
                  <small className="muted">单声道或双声道、16-bit PCM，0.1–600 秒，最大 20 MiB。</small>
                </div>
                <div className="card-actions">
                  <button className="button button-small" disabled={saving}>
                    {saving ? "上传中…" : "上传预设语音"}
                  </button>
                </div>
              </form>
              <div className="table-wrap" role="region" aria-label="阶段预设语音表格" tabIndex={0} style={{ marginTop: 18 }}>
                <table>
                  <thead><tr><th>阶段 key</th><th>名称与文本</th><th>音频</th><th>状态</th><th>操作</th></tr></thead>
                  <tbody>
                    {audioCues.map((item) => (
                      <tr key={item.id}>
                        <td><code>{item.key}</code></td>
                        <td><strong>{item.name}</strong><small className="muted">{item.text || "未填写文本说明"}</small></td>
                        <td>{item.audio_url ? "WAV 已上传" : "缺少音频"}</td>
                        <td>{item.is_active ? "启用" : "停用"}</td>
                        <td><button className="text-button" disabled={saving} onClick={() => void patchAudioCue(item.id, { is_active: !item.is_active })}>{item.is_active ? "停用" : "启用"}</button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {!audioCues.length && <div className="empty">尚未上传阶段预设语音</div>}
              </div>
            </>
          )}
          {tab === "media" && (
            <>
              <div className="panel-title">
                <h2>媒体存储与一致性</h2>
                <span className="badge">默认只读盘点</span>
              </div>
              {media ? (
                <>
                  <div className="stats-grid">
                    <div className="stat-card">
                      <span className="muted">媒体文件</span>
                      <strong>{media.scanned_files}</strong>
                      <small>{bytes(media.scanned_bytes)}</small>
                    </div>
                    <div className="stat-card">
                      <span className="muted">有效引用</span>
                      <strong>
                        {media.referenced_existing_files}/
                        {media.referenced_files}
                      </strong>
                    </div>
                    <div className="stat-card">
                      <span className="muted">孤儿候选</span>
                      <strong>{media.orphan_candidate_count}</strong>
                      <small>{bytes(media.orphan_candidate_bytes)}</small>
                    </div>
                    <div className="stat-card">
                      <span className="muted">磁盘剩余</span>
                      <strong>{bytes(media.disk_free_bytes)}</strong>
                    </div>
                  </div>
                  <div className="hero-actions" style={{ marginTop: 18 }}>
                    <button
                      className="button button-secondary"
                      disabled={saving}
                      onClick={() => void refreshMedia()}
                    >
                      <RefreshCw size={16} />
                      重新盘点
                    </button>
                    <button
                      className="button button-danger"
                      disabled={
                        saving ||
                        media.orphan_candidate_count === 0 ||
                        media.truncated
                      }
                      onClick={() => void cleanupMedia()}
                    >
                      <Trash2 size={16} />
                      清理 24 小时以上孤儿
                    </button>
                  </div>
                  {media.truncated && (
                    <div className="error-box" role="alert">
                      文件数量超过安全扫描上限，本次禁止执行清理。
                    </div>
                  )}
                  {media.unsafe_entries > 0 && (
                    <div className="error-box" role="alert">
                      发现 {media.unsafe_entries}{" "}
                      个符号链接或不安全条目，系统已跳过。
                    </div>
                  )}
                  <h3 style={{ marginTop: 26 }}>缺失引用</h3>
                  <div className="history-list">
                    {media.missing_references.map((item) => (
                      <div className="history-row" key={item}>
                        {item}
                      </div>
                    ))}
                    {media.missing_reference_count === 0 && (
                      <div className="empty">数据库引用的媒体文件均存在</div>
                    )}
                  </div>
                  <h3 style={{ marginTop: 26 }}>可清理候选</h3>
                  <div className="table-wrap" role="region" aria-label="媒体清理候选表格" tabIndex={0}>
                    <table>
                      <thead>
                        <tr>
                          <th>相对路径</th>
                          <th>大小</th>
                          <th>最后修改</th>
                        </tr>
                      </thead>
                      <tbody>
                        {media.orphan_candidates.map((item) => (
                          <tr key={item.path}>
                            <td>{item.path}</td>
                            <td>{bytes(item.bytes)}</td>
                            <td>
                              {new Date(item.modified_at).toLocaleString(
                                "zh-CN",
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    {media.orphan_candidate_count === 0 && (
                      <div className="empty">没有超过 24 小时的孤儿媒体</div>
                    )}
                  </div>
                </>
              ) : (
                <div className="empty">正在盘点媒体存储…</div>
              )}
              <div className="panel-title" style={{ marginTop: 36 }}>
                <h2>比赛归档与校验</h2>
                <span className="badge">终局记录</span>
              </div>
              {archives ? (
                <>
                  <div className="stats-grid">
                    <div className="stat-card">
                      <span className="muted">应归档比赛</span>
                      <strong>{archives.expected_matches}</strong>
                    </div>
                    <div className="stat-card">
                      <span className="muted">校验完整</span>
                      <strong>
                        {archives.complete_archives}/{archives.expected_matches}
                      </strong>
                    </div>
                    <div className="stat-card">
                      <span className="muted">缺失或损坏</span>
                      <strong>{archives.invalid_archive_count}</strong>
                    </div>
                    <div className="stat-card">
                      <span className="muted">孤儿候选</span>
                      <strong>{archives.orphan_candidate_count}</strong>
                      <small>{bytes(archives.orphan_candidate_bytes)}</small>
                    </div>
                  </div>
                  <div className="hero-actions" style={{ marginTop: 18 }}>
                    <button
                      className="button button-secondary"
                      disabled={saving}
                      onClick={() => void refreshArchives()}
                    >
                      <RefreshCw size={16} />
                      盘点比赛归档
                    </button>
                    <button
                      className="button"
                      disabled={
                        saving ||
                        archives.invalid_archive_count === 0 ||
                        archives.truncated
                      }
                      onClick={() => void repairArchives()}
                    >
                      <RefreshCw size={16} />
                      修复缺失或损坏归档
                    </button>
                    <button
                      className="button button-danger"
                      disabled={
                        saving ||
                        archives.orphan_candidate_count === 0 ||
                        archives.truncated
                      }
                      onClick={() => void cleanupArchives()}
                    >
                      <Trash2 size={16} />
                      清理孤儿归档
                    </button>
                  </div>
                  {(archives.truncated || archives.unsafe_entries > 0) && (
                    <div className="error-box" role="alert">
                      {archives.truncated
                        ? "归档文件数量超过安全扫描上限，修复和清理已禁用。"
                        : `发现 ${archives.unsafe_entries} 个不安全条目，系统已跳过。`}
                    </div>
                  )}
                  <h3 style={{ marginTop: 26 }}>缺失或校验失败</h3>
                  <div className="history-list">
                    {archives.invalid_archives.map((item) => (
                      <div className="history-row" key={item.match_id}>
                        <code>{item.match_id}</code>
                        <span>{item.reason}</span>
                      </div>
                    ))}
                    {archives.invalid_archive_count === 0 && (
                      <div className="empty">全部终局比赛归档均通过校验</div>
                    )}
                  </div>
                  <h3 style={{ marginTop: 26 }}>归档清理候选</h3>
                  <div className="table-wrap" role="region" aria-label="归档清理候选表格" tabIndex={0}>
                    <table>
                      <thead>
                        <tr>
                          <th>文件</th>
                          <th>大小</th>
                          <th>最后修改</th>
                        </tr>
                      </thead>
                      <tbody>
                        {archives.orphan_candidates.map((item) => (
                          <tr key={item.path}>
                            <td>{item.path}</td>
                            <td>{bytes(item.bytes)}</td>
                            <td>
                              {new Date(item.modified_at).toLocaleString(
                                "zh-CN",
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    {archives.orphan_candidate_count === 0 && (
                      <div className="empty">没有超过 24 小时的孤儿归档</div>
                    )}
                  </div>
                </>
              ) : (
                <div className="empty">正在盘点比赛归档…</div>
              )}
            </>
          )}
          {tab === "reviews" && (
            <>
              <div className="panel-title">
                <h2>比赛结果复核</h2>
                <span className="badge">{reviews.length} 项待处理</span>
              </div>
              <div className="history-list">
                {reviews.map((item) => (
                  <div
                    className="history-row"
                    style={{ gridTemplateColumns: "90px 1fr auto" }}
                    key={item.scorecard_id}
                  >
                    <span className="room-code">#{item.room_code}</span>
                    <span className="row-main">
                      <strong>{item.topic}</strong>
                      <small>{item.reason}</small>
                    </span>
                    <div className="card-actions">
                      <button
                        className="button button-small button-secondary"
                        disabled={retryingReviewIds.has(item.scorecard_id)}
                        onClick={() => void retryJudge(item)}
                      >
                        {retryingReviewIds.has(item.scorecard_id) ? "正在重新排队…" : "重试 AI 裁判"}
                      </button>
                      <button
                        className="button button-small"
                        disabled={retryingReviewIds.has(item.scorecard_id)}
                        onClick={() => review(item)}
                      >
                        复核判定
                      </button>
                    </div>
                  </div>
                ))}
                {!reviews.length && (
                  <div className="empty">没有等待复核的比赛</div>
                )}
              </div>
              <div className="panel-title" style={{ marginTop: 30 }}>
                <h2>近期已确认赛果</h2>
                <span className="muted">修正会追加积分补偿与审计记录</span>
              </div>
              <div className="history-list">
                {recentReviews.map((item) => (
                  <div
                    className="history-row"
                    style={{ gridTemplateColumns: "90px 1fr auto" }}
                    key={item.scorecard_id}
                  >
                    <span className="room-code">#{item.room_code}</span>
                    <span className="row-main">
                      <strong>{item.topic}</strong>
                      <small>
                        {item.winner === "aff"
                          ? "正方胜"
                          : item.winner === "neg"
                            ? "反方胜"
                            : "平局"}{" "}
                        · 正方 {item.affirmative_score.toFixed(1)} / 反方{" "}
                        {item.negative_score.toFixed(1)} · {item.reason}
                      </small>
                    </span>
                    <button
                      className="button button-small button-secondary"
                      onClick={() => review(item, "correct")}
                    >
                      修正结果
                    </button>
                  </div>
                ))}
                {!recentReviews.length && (
                  <div className="empty">还没有可修正的已确认赛果</div>
                )}
              </div>
            </>
          )}
          {tab === "audit" && (
            <>
              <div className="panel-title">
                <h2>管理员审计日志</h2>
                <span className="muted">
                  {auditLoading && !audit.length
                    ? "正在载入审计记录…"
                    : `共 ${auditPagination.total} 条`}
                </span>
              </div>
              <form
                className="admin-filter-row"
                onSubmit={(event) => {
                  event.preventDefault();
                  void loadAudit(1, auditQuery);
                }}
              >
                <input
                  className="input"
                  aria-label="搜索操作或目标"
                  placeholder="搜索操作、目标类型或 ID"
                  maxLength={120}
                  value={auditQuery}
                  onChange={(event) => setAuditQuery(event.target.value)}
                />
                <button className="button button-small" disabled={auditLoading}>
                  {auditLoading ? "搜索中…" : "搜索"}
                </button>
              </form>
              <div className="table-wrap" role="region" aria-label="审计日志表格" tabIndex={0}>
                <table>
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>管理员</th>
                      <th>操作</th>
                      <th>目标</th>
                      <th>详情</th>
                    </tr>
                  </thead>
                  <tbody>
                    {audit.map((item) => (
                      <tr key={item.id}>
                        <td>
                          {new Date(item.created_at).toLocaleString("zh-CN")}
                        </td>
                        <td>{item.actor_name}</td>
                        <td>{auditActionLabels[item.action] || item.action}</td>
                        <td>
                          {auditTargetLabels[item.target_type] || item.target_type} · {item.target_id}
                        </td>
                        <td>
                          <details>
                            <summary>查看</summary>
                            <pre className="audit-json">
                              {JSON.stringify(item.payload, null, 2)}
                            </pre>
                          </details>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {!auditLoading && !audit.length && (
                  <div className="empty">没有符合条件的审计记录</div>
                )}
              </div>
              <AdminPagination
                label="审计日志"
                page={auditPagination.page}
                pages={auditPagination.pages}
                loading={auditLoading}
                onPageChange={(page) => void loadAudit(page)}
              />
            </>
          )}
        </section>
      </div>
      {topicCompetition && (
        <div
          className="dialog-overlay"
          role="presentation"
          onMouseDown={(event) =>
            event.target === event.currentTarget && setTopicCompetition(null)
          }
        >
          <form
            className="dialog"
            role="dialog"
            aria-modal="true"
            aria-label="添加赛事题目"
            onSubmit={submitTopic}
          >
            <div className="dialog-head">
              <div>
                <span className="eyebrow">Topic Library</span>
                <h2>添加辩题</h2>
              </div>
              <button
                type="button"
                className="icon-button"
                aria-label="关闭"
                onClick={() => setTopicCompetition(null)}
              >
                ×
              </button>
            </div>
            <p className="muted">
              将题目加入“{topicCompetition.name}”题库，之后创建房间时即可选择。
            </p>
            <div className="field">
              <label htmlFor="admin-topic-title">辩题内容</label>
              <textarea
                id="admin-topic-title"
                className="textarea"
                minLength={4}
                maxLength={300}
                required
                autoFocus
                value={topicTitle}
                onChange={(event) => setTopicTitle(event.target.value)}
                placeholder="例如：人工智能时代，还要不要学编程？"
              />
            </div>
            <div className="dialog-actions">
              <button
                type="button"
                className="button button-secondary"
                onClick={() => setTopicCompetition(null)}
              >
                取消
              </button>
              <button
                className="button"
                disabled={saving || topicTitle.trim().length < 4}
              >
                {saving ? "保存中…" : "保存题目"}
              </button>
            </div>
          </form>
        </div>
      )}
      {passwordResetTarget && (
        <div
          className="dialog-overlay"
          role="presentation"
          onMouseDown={(event) =>
            event.target === event.currentTarget && setPasswordResetTarget(null)
          }
        >
          <form
            className="dialog"
            role="dialog"
            aria-modal="true"
            aria-label="重置用户密码"
            onSubmit={resetPassword}
          >
            <div className="dialog-head">
              <div>
                <span className="eyebrow">Account Recovery</span>
                <h2>重置用户密码</h2>
              </div>
              <button
                type="button"
                className="icon-button"
                aria-label="关闭"
                onClick={() => setPasswordResetTarget(null)}
              >
                ×
              </button>
            </div>
            <p className="muted">
              为 {passwordResetTarget.real_name}（@{passwordResetTarget.account}
              ）设置临时密码。提交后，该用户的所有已登录设备会立即退出。
            </p>
            <div className="notice-box">
              审计日志只记录会话撤销数量，不会保存或展示新密码。请通过安全方式将密码告知用户，并提醒其登录后自行修改。
            </div>
            <div className="field">
              <label htmlFor="admin-reset-password">新密码</label>
              <input
                id="admin-reset-password"
                className="input"
                type="password"
                autoComplete="new-password"
                minLength={8}
                maxLength={128}
                required
                autoFocus
                value={passwordResetForm.new_password}
                onChange={(event) =>
                  setPasswordResetForm((value) => ({
                    ...value,
                    new_password: event.target.value,
                  }))
                }
              />
            </div>
            <div className="field">
              <label htmlFor="admin-reset-password-confirm">确认新密码</label>
              <input
                id="admin-reset-password-confirm"
                className="input"
                type="password"
                autoComplete="new-password"
                minLength={8}
                maxLength={128}
                required
                value={passwordResetForm.confirm_password}
                onChange={(event) =>
                  setPasswordResetForm((value) => ({
                    ...value,
                    confirm_password: event.target.value,
                  }))
                }
              />
            </div>
            <div className="dialog-actions">
              <button
                type="button"
                className="button button-secondary"
                onClick={() => setPasswordResetTarget(null)}
              >
                取消
              </button>
              <button
                className="button"
                disabled={
                  saving ||
                  passwordResetForm.new_password.length < 8 ||
                  passwordResetForm.new_password !==
                    passwordResetForm.confirm_password
                }
              >
                {saving ? "重置中…" : "确认重置"}
              </button>
            </div>
          </form>
        </div>
      )}
      {templateTarget && (
        <div
          className="dialog-overlay"
          role="presentation"
          onMouseDown={(event) =>
            event.target === event.currentTarget && setTemplateTarget(null)
          }
        >
          <form
            className="dialog dialog-wide"
            role="dialog"
            aria-modal="true"
            aria-label="创建自动流程新版本"
            onSubmit={submitTemplateVersion}
          >
            <div className="dialog-head">
              <div>
                <span className="eyebrow">Versioned Automation</span>
                <h2>创建自动流程新版本</h2>
              </div>
              <button
                type="button"
                className="icon-button"
                aria-label="关闭"
                onClick={() => setTemplateTarget(null)}
              >
                ×
              </button>
            </div>
            <div className="notice-box">
              源版本：{templateTarget.name} v{templateTarget.version}
              。保存后只影响新创建房间，进行中和历史房间继续使用原快照。
            </div>
            <div className="field">
              <label htmlFor="template-name">模板名称</label>
              <input
                id="template-name"
                className="input"
                maxLength={120}
                required
                value={templateName}
                onChange={(event) => setTemplateName(event.target.value)}
              />
            </div>
            <div className="field">
              <label>绑定到赛事</label>
              <div className="seat-picker">
                {competitions.map((competition) => (
                  <label
                    className={`seat-option ${templateCompetitionIds.includes(competition.id) ? "selected" : ""}`}
                    key={competition.id}
                  >
                    <input
                      type="checkbox"
                      checked={templateCompetitionIds.includes(competition.id)}
                      onChange={(event) =>
                        setTemplateCompetitionIds((current) =>
                          event.target.checked
                            ? [...current, competition.id]
                            : current.filter((id) => id !== competition.id),
                        )
                      }
                    />
                    <strong>{competition.name}</strong>
                    <small>
                      {competition.format} · {competition.seat_count} 席
                    </small>
                  </label>
                ))}
              </div>
            </div>
            <div className="field">
              <label htmlFor="template-stages">阶段 JSON</label>
              <textarea
                id="template-stages"
                className="textarea code-textarea"
                rows={18}
                required
                value={templateStages}
                onChange={(event) => setTemplateStages(event.target.value)}
                spellCheck={false}
              />
              <small className="muted">
                最后阶段必须为 judging；speech 需要 seat；free 需要 side 和
                turn_duration。
              </small>
            </div>
            <div className="dialog-actions">
              <button
                type="button"
                className="button button-secondary"
                onClick={() => setTemplateTarget(null)}
              >
                取消
              </button>
              <button
                className="button"
                disabled={
                  saving || !templateName.trim() || !templateStages.trim()
                }
              >
                {saving ? "创建中…" : "创建并绑定新版本"}
              </button>
            </div>
          </form>
        </div>
      )}
      {reviewTarget && (
        <div
          className="dialog-overlay"
          role="presentation"
          onMouseDown={(event) =>
            event.target === event.currentTarget && setReviewTarget(null)
          }
        >
          <form
            className="dialog"
            role="dialog"
            aria-modal="true"
            aria-label={
              reviewMode === "correct" ? "修正比赛结果" : "复核比赛结果"
            }
            onSubmit={submitReview}
          >
            <div className="dialog-head">
              <div>
                <span className="eyebrow">
                  {reviewMode === "correct"
                    ? "Result Correction"
                    : "Judge Review"}
                </span>
                <h2>
                  {reviewMode === "correct" ? "修正比赛结果" : "复核比赛结果"}
                </h2>
              </div>
              <button
                type="button"
                className="icon-button"
                aria-label="关闭"
                onClick={() => setReviewTarget(null)}
              >
                ×
              </button>
            </div>
            <p className="muted">
              房间 #{reviewTarget.room_code} · {reviewTarget.topic}
            </p>
            {reviewMode === "correct" && (
              <div className="notice-box">
                原始积分记录不会被覆盖；系统将追加补偿记录并保留完整审计轨迹。
              </div>
            )}
            <div className="review-form-grid">
              <div className="field">
                <label htmlFor="review-winner">胜方</label>
                <select
                  id="review-winner"
                  className="select"
                  value={reviewForm.winner}
                  onChange={(event) =>
                    setReviewForm((value) => ({
                      ...value,
                      winner: event.target.value,
                    }))
                  }
                >
                  <option value="aff">正方</option>
                  <option value="neg">反方</option>
                  <option value="draw">平局</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="review-aff-score">正方评分</label>
                <input
                  id="review-aff-score"
                  className="input"
                  type="number"
                  min={0}
                  max={100}
                  step={0.1}
                  value={reviewForm.affirmative_score}
                  onChange={(event) =>
                    setReviewForm((value) => ({
                      ...value,
                      affirmative_score: Number(event.target.value),
                    }))
                  }
                />
              </div>
              <div className="field">
                <label htmlFor="review-neg-score">反方评分</label>
                <input
                  id="review-neg-score"
                  className="input"
                  type="number"
                  min={0}
                  max={100}
                  step={0.1}
                  value={reviewForm.negative_score}
                  onChange={(event) =>
                    setReviewForm((value) => ({
                      ...value,
                      negative_score: Number(event.target.value),
                    }))
                  }
                />
              </div>
            </div>
            <div className="field">
              <label htmlFor="review-reasoning">
                {reviewMode === "correct" ? "修正理由" : "复核理由"}
              </label>
              <textarea
                id="review-reasoning"
                className="textarea"
                minLength={2}
                maxLength={5000}
                required
                value={reviewForm.reasoning}
                onChange={(event) =>
                  setReviewForm((value) => ({
                    ...value,
                    reasoning: event.target.value,
                  }))
                }
              />
            </div>
            <div className="dialog-actions">
              <button
                type="button"
                className="button button-secondary"
                onClick={() => setReviewTarget(null)}
              >
                取消
              </button>
              <button
                className="button"
                disabled={saving || reviewForm.reasoning.trim().length < 2}
              >
                {saving
                  ? "提交中…"
                  : reviewMode === "correct"
                    ? "确认修正"
                    : "确认判定"}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
