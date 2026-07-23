"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import {
  Bot,
  Check,
  Clock3,
  DoorOpen,
  Eye,
  Play,
  Share2,
  ShieldCheck,
  Trash2,
  UserPlus,
  Users,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiRequestError, apiFetch } from "@/lib/api";
import { LoadError } from "@/components/load-error";
import { PageLoading } from "@/components/page-loading";
import { MicrophonePreflight } from "@/components/microphone-preflight";
import { competitionDisplayName } from "@/lib/primary-competition";
import { acquireControlLease, controlLeaseFor, isControlLeaseConflict } from "@/lib/control-lease";
import { roomCreationBlockedReason, seasonStatusLabel } from "@/lib/seasons";
import { useRoom } from "@/lib/use-room";
import { useSession } from "@/lib/use-session";
import type { Room } from "@/lib/types";

import styles from "../room-operations.module.css";

type UnconfirmedLobbyAction = {
  path: string;
  body: Record<string, unknown>;
  message: string;
};

function lobbyActionApplied(
  action: Pick<UnconfirmedLobbyAction, "path" | "body">,
  room: Room | null,
) {
  if (!room) return false;
  const me = room.seats.find((seat) => seat.is_me);
  if (action.path === "claim-seat")
    return room.my_seat === action.body.seat_key;
  if (action.path === "release-seat") return !room.my_seat;
  if (action.path === "ready")
    return Boolean(me?.is_ready) === Boolean(action.body.ready);
  if (action.path === "start") return room.status !== "lobby";
  if (action.path === "cancel") return room.status === "cancelled";
  const removedSeat = action.path.match(/^seats\/([^/]+)\/remove$/)?.[1];
  return Boolean(
    removedSeat &&
    room.seats.find((seat) => seat.seat_key === removedSeat)?.occupant_type ===
      "open",
  );
}

export default function LobbyPage() {
  const { code } = useParams<{ code: string }>();
  const router = useRouter();
  const { user, loading: sessionLoading } = useSession();
  const {
    room,
    setRoom,
    connected,
    error: roomError,
    reconnect,
  } = useRoom(code);
  const [error, setError] = useState("");
  const [unconfirmedAction, setUnconfirmedAction] =
    useState<UnconfirmedLobbyAction | null>(null);
  const [busy, setBusy] = useState("");
  const [copied, setCopied] = useState(false);
  const [confirmingStart, setConfirmingStart] = useState(false);
  const [deviceControl, setDeviceControl] = useState<
    "unavailable" | "acquiring" | "owned" | "lost"
  >("unavailable");
  const [controlSeq, setControlSeq] = useState<number | null>(null);
  const attempts = useRef<Record<string, { fingerprint: string; key: string }>>(
    {},
  );
  const previousMySeat = useRef<string | null>(room?.my_seat || null);
  const currentRoom = useRef<Room | null>(room);
  currentRoom.current = room;
  const startButton = useRef<HTMLButtonElement | null>(null);
  const startConfirmCancel = useRef<HTMLButtonElement | null>(null);
  const startConfirmDialog = useRef<HTMLDivElement | null>(null);
  const seatSection = useRef<HTMLElement | null>(null);
  const mySeatKey = room?.my_seat || "";
  const roomStatus = room?.status || "";
  const controlLease = useMemo(
    () => controlLeaseFor(code, mySeatKey),
    [code, mySeatKey],
  );
  const acquireDeviceControl = useCallback(
    async (force = false) => {
      if (roomStatus !== "lobby" || !mySeatKey || !controlLease) {
        setDeviceControl("unavailable");
        return;
      }
      setDeviceControl("acquiring");
      try {
        const result = await acquireControlLease(code, controlLease, force);
        setControlSeq(result.seq);
        setDeviceControl("owned");
        setError("");
      } catch (reason) {
        const conflict = isControlLeaseConflict(reason);
        setDeviceControl(conflict ? "lost" : "unavailable");
        setError(conflict
          ? reason instanceof Error ? reason.message : "该席位已由另一设备控制。"
          : "设备控制绑定暂时失败，请检查网络并重试；当前页面不会自动接管其他设备。",
        );
      }
    },
    [code, controlLease, mySeatKey, roomStatus],
  );
  useEffect(() => {
    void acquireDeviceControl(false);
  }, [acquireDeviceControl]);
  useEffect(() => {
    if (!room?.my_seat || deviceControl !== "owned" || controlSeq === null)
      return;
    const takenOver = room.recent_events.some(
      (event) =>
        event.seq > controlSeq &&
        event.type === "seat.control_taken_over" &&
        event.payload.seat_key === room.my_seat,
    );
    if (takenOver) {
      setDeviceControl("lost");
      setError("该席位已由另一设备接管，当前页面已切换为只读。");
    }
  }, [controlSeq, deviceControl, room?.my_seat, room?.recent_events]);
  useEffect(() => {
    if (unconfirmedAction && lobbyActionApplied(unconfirmedAction, room)) {
      delete attempts.current[unconfirmedAction.path];
      setUnconfirmedAction(null);
    }
  }, [room, unconfirmedAction]);
  useEffect(() => {
    const previous = previousMySeat.current;
    if (room && previous && !room.my_seat) {
      const removed = room.recent_events.some(
        (event) =>
          event.type === "seat.removed_by_owner" &&
          event.payload.seat_key === previous,
      );
      if (removed)
        setError(
          "房主已在开赛前调整你的席位。你可以重新认领空席，或返回赛事大厅加入其他比赛。",
        );
    }
    previousMySeat.current = room?.my_seat || null;
  }, [room]);
  useEffect(() => {
    if (!room || room.status === "lobby") return;
    if (room.status === "cancelled") router.replace(`/?room_closed=${code}`);
    else if (
      ["completed", "review_required", "terminated"].includes(room.status)
    )
      router.replace(`/rooms/${code}/result`);
    else router.replace(`/rooms/${code}/debate`);
  }, [room, code, router]);
  useEffect(() => {
    if (!confirmingStart) return;
    const previousBodyOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    startConfirmCancel.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setConfirmingStart(false);
        requestAnimationFrame(() => startButton.current?.focus());
        return;
      }
      if (event.key !== "Tab" || !startConfirmDialog.current) return;
      const focusable = [
        ...startConfirmDialog.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    const keepFocusInside = (event: FocusEvent) => {
      if (
        !startConfirmDialog.current ||
        startConfirmDialog.current.contains(event.target as Node)
      )
        return;
      const firstFocusable =
        startConfirmDialog.current.querySelector<HTMLElement>(
          'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        );
      (firstFocusable || startConfirmCancel.current)?.focus();
    };
    document.addEventListener("keydown", closeOnEscape);
    document.addEventListener("focusin", keepFocusInside);
    return () => {
      document.removeEventListener("keydown", closeOnEscape);
      document.removeEventListener("focusin", keepFocusInside);
      document.body.style.overflow = previousBodyOverflow;
    };
  }, [confirmingStart]);
  async function action(path: string, body: object = {}) {
    setBusy(path);
    setError("");
    const fingerprint = JSON.stringify(body);
    const previous = attempts.current[path];
    if (!previous || previous.fingerprint !== fingerprint)
      attempts.current[path] = { fingerprint, key: crypto.randomUUID() };
    try {
      const data = await apiFetch<{ room: Room }>(
        `/api/rooms/${code}/${path}`,
        {
          method: "POST",
          headers: { "X-Idempotency-Key": attempts.current[path].key },
          body: JSON.stringify(body),
        },
      );
      delete attempts.current[path];
      setUnconfirmedAction(null);
      setRoom((current) =>
        !current || data.room.seq >= current.seq ? data.room : current,
      );
      if (path === "start") router.push(`/rooms/${code}/debate`);
      if (path === "cancel") router.push("/");
      return data.room;
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 0) {
        const uncertain = {
          path,
          body: body as Record<string, unknown>,
          message: err.message,
        };
        // The response can be lost after the server has committed the write.
        // A newer socket snapshot may therefore confirm the result before the
        // rejected fetch settles; never resurrect a stale warning in that race.
        if (!lobbyActionApplied(uncertain, currentRoom.current))
          setUnconfirmedAction(uncertain);
        else delete attempts.current[path];
      } else {
        setError(err instanceof Error ? err.message : "操作失败");
      }
    } finally {
      setBusy("");
    }
  }
  async function copyInvite() {
    try {
      if (!navigator.clipboard) throw new Error("当前浏览器不支持剪贴板");
      await navigator.clipboard.writeText(location.href);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      setError(err instanceof Error ? err.message : "复制邀请链接失败");
    }
  }
  if (!room && roomError)
    return <LoadError message={roomError} retry={reconnect} />;
  if (!room) return <PageLoading label="正在进入比赛房间…" />;
  if (room.status !== "lobby") return <PageLoading label="正在返回当前比赛…" />;
  const me = room.seats.find((seat) => seat.is_me);
  const deviceCanWrite = !me || deviceControl === "owned";
  const humans = room.seats.filter((seat) => seat.occupant_type === "human");
  const seatIsConnected = (seat: (typeof room.seats)[number]) =>
    seat.is_me ? connected : seat.connected;
  const readyHumans = humans.filter((seat) => seat.is_ready).length;
  const connectedHumans = humans.filter(seatIsConnected).length;
  const offlineHumans = humans.filter((seat) => !seatIsConnected(seat));
  const openSeats = room.seats.filter(
    (seat) => seat.occupant_type === "open",
  ).length;
  const removableHumans = humans.filter(
    (seat) => !seat.is_owner && !seat.is_me,
  );
  const unreadyHumans = humans.filter((seat) => !seat.is_ready);
  const seasonBlockedReason = roomCreationBlockedReason({
    ...room.competition,
    season: room.season,
  });
  const startBlockedReason = seasonBlockedReason
    ? seasonBlockedReason
    : !deviceCanWrite
      ? deviceControl === "acquiring"
        ? "正在绑定当前设备，请稍候。"
        : deviceControl === "lost"
        ? "当前席位由另一设备控制，请先接管到本设备。"
        : "当前设备控制绑定暂时失败，请重试后再开始。"
      : offlineHumans.length
        ? `还需等待 ${offlineHumans.map((seat) => `${seat.display_name}（${seat.label}）`).join("、")} 返回房间。全部真人在线后才能开始，避免开场后立即进入断线等待。`
        : unreadyHumans.length
        ? `还需等待 ${unreadyHumans.map((seat) => `${seat.display_name}（${seat.label}）`).join("、")} 确认准备。`
        : "所有真人辩手已准备。开始后席位将锁定，空席由 AI 补齐，自动流程随后直接开场。";
  const nextStep = !me
    ? {
        title: user ? "下一步：选择一个空席" : "下一步：登录后选择席位",
        detail: user
          ? "认领后确认准备；比赛开始时其余空席会自动由 AI 补齐。"
          : "登录后会返回当前房间，不需要重新输入房间号。",
      }
    : !deviceCanWrite
      ? {
          title: deviceControl === "acquiring"
            ? "正在绑定当前设备"
            : deviceControl === "lost"
              ? "当前页面为只读"
              : "设备绑定暂时失败",
          detail: deviceControl === "acquiring"
            ? "正在确认本浏览器的席位控制权，完成前不会提交准备或开赛操作。"
            : deviceControl === "lost"
              ? "该席位已在另一设备打开。确认现场无人使用旧设备后，可接管到本设备。"
              : "这通常是短暂的网络或房间操作冲突，不代表其他设备接管；请重新绑定当前设备。",
        }
      : !me.is_ready
        ? {
            title: "下一步：确认准备",
            detail:
              "建议先完成下方麦克风检查，再确认准备。麦克风检查不会录音或上传。",
          }
        : room.can_control &&
            !offlineHumans.length &&
            !unreadyHumans.length
          ? {
              title: "下一步：开始自动比赛",
              detail:
                "只需点击一次。进入舞台后系统会建立实时语音并播放预设开场提示，准备期间不会开始比赛计时。",
            }
          : room.can_control
            ? { title: "等待其他真人确认准备", detail: startBlockedReason }
            : {
                title: "你已准备",
                detail:
                  "请保持本页打开。房主开始后会自动进入比赛舞台；如需调整，可先在房间信息区取消准备。",
              };
  const setupSteps = [
    {
      label: room.can_control ? "邀请队友" : "进入房间",
      detail: room.can_control ? "分享房间号即可" : "房间信息已同步",
      state: "done",
    },
    {
      label: "选择席位",
      detail: me ? `${me.label} · ${me.display_name}` : "从下方选择空席",
      state: me ? "done" : "current",
    },
    {
      label: "确认准备",
      detail: me?.is_ready
        ? "你的准备已确认"
        : me
          ? "检查设备后确认"
          : "选择席位后进行",
      state: me?.is_ready ? "done" : me ? "current" : "upcoming",
    },
    {
      label: "开始比赛",
      detail: room.can_control
        ? offlineHumans.length
          ? `还有 ${offlineHumans.length} 位真人离线`
          : unreadyHumans.length
          ? `还差 ${unreadyHumans.length} 位真人`
          : "确认后自动开场"
        : "由房主确认开始",
      state: me?.is_ready ? "current" : "upcoming",
    },
  ] as const;
  const startDisabled =
    Boolean(busy) ||
    !deviceCanWrite ||
    Boolean(seasonBlockedReason) ||
    offlineHumans.length > 0 ||
    unreadyHumans.length > 0;
  const primaryDisabled = me
    ? me.is_ready
      ? room.can_control
        ? startDisabled
        : true
      : Boolean(busy) || !deviceCanWrite || Boolean(seasonBlockedReason)
    : false;
  const primaryLabel = !me
    ? user
      ? "选择参赛席位"
      : "登录后选择席位"
    : !me.is_ready
      ? busy === "ready"
        ? "正在确认…"
        : seasonBlockedReason
          ? "赛季已关闭"
          : "确认准备"
      : room.can_control
        ? busy === "start"
          ? "正在进入比赛…"
          : offlineHumans.length
            ? `等待 ${offlineHumans.length} 位真人上线`
            : unreadyHumans.length
            ? `等待 ${unreadyHumans.length} 位真人准备`
            : "开始比赛"
        : busy === "ready"
          ? "正在取消准备…"
          : "等待房主开始";
  function runPrimaryAction() {
    if (!me) {
      if (!user) {
        router.push(
          `/login?next=${encodeURIComponent(`/rooms/${code}/lobby`)}`,
        );
        return;
      }
      seatSection.current?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
      seatSection.current?.focus({ preventScroll: true });
      return;
    }
    if (!me.is_ready) {
      void action("ready", { ready: true });
      return;
    }
    if (room?.can_control && !startDisabled) setConfirmingStart(true);
  }
  return (
    <div className={`page-shell ${styles.lobbyPage}`}>
      <div className="section-head">
        <div>
          <span className="eyebrow">赛前大厅</span>
          <h1>
            比赛房间 <span className="room-code">#{room.code}</span>
          </h1>
          <p>{competitionDisplayName(room.competition)} · 等待辩手准备</p>
        </div>
        <div className={`connection ${connected ? "ok" : ""}`}>
          {connected ? "实时连接" : "正在重连"}
        </div>
      </div>
      {roomError && !connected && (
        <div className="warning-box" role="alert">
          {roomError}
          <button type="button" className="text-button" onClick={reconnect}>
            立即重连
          </button>
        </div>
      )}
      <div className="panel lobby-topic">
        <span className="badge">本场辩题</span>
        <h2>{room.topic}</h2>
        <div className="detail-meta" role="status" aria-live="polite">
          <span>
            <Users size={15} />
            {readyHumans} / {humans.length} 位真人已准备
          </span>
          <span>
            <Check size={15} />
            {connectedHumans} 位真人在线
          </span>
          <span>
            <Bot size={15} />
            {openSeats} 个空席将由 AI 补齐
          </span>
        </div>
      </div>
      <ol className={styles.setupSteps} aria-label="开赛准备进度">
        {setupSteps.map((step, index) => (
          <li
            key={step.label}
            className={styles[step.state]}
            aria-current={step.state === "current" ? "step" : undefined}
          >
            <span className={styles.stepMarker} aria-hidden="true">
              {step.state === "done" ? <Check size={15} /> : index + 1}
            </span>
            <span>
              <strong>{step.label}</strong>
              <small>{step.detail}</small>
            </span>
          </li>
        ))}
      </ol>
      {seasonBlockedReason && (
        <div className="warning-box" style={{ marginBottom: 18 }}>
          <strong>{seasonStatusLabel(room.season)}</strong> ·{" "}
          {seasonBlockedReason} 该房间仍可观战、释放席位或关闭。
        </div>
      )}
      <div className="lobby-layout">
        <section
          ref={seatSection}
          className={`panel ${styles.seatPanel}`}
          tabIndex={-1}
        >
          <div className="panel-title">
            <h2>选择与确认席位</h2>
            <span className="muted">
              {seasonBlockedReason
                ? "当前赛季不可继续加入"
                : me
                  ? room.can_control
                    ? "确认双方人员与准备状态"
                    : "查看双方席位与准备状态"
                  : "点击空席加入"}
            </span>
          </div>
          <div className="lobby-teams">
            {(["aff", "neg"] as const).map((side) => {
              const sideSeats = room.seats.filter((seat) => seat.side === side);
              const sideHumans = sideSeats.filter(
                (seat) => seat.occupant_type === "human",
              ).length;
              const sideName = side === "aff" ? "正方" : "反方";
              return (
                <div
                  className={`lobby-team ${side}`}
                  key={side}
                  role="group"
                  aria-label={`${sideName}席位`}
                >
                  <h3>
                    <span>{sideName}</span>
                    <small>
                      {sideHumans} 位真人 · {sideSeats.length} 个席位
                    </small>
                  </h3>
                  {sideSeats.map((seat) => (
                    <button
                      className={`lobby-seat ${seat.occupant_type !== "open" ? "occupied" : ""} ${seat.is_me ? "me" : ""}`}
                      key={seat.seat_key}
                      disabled={
                        Boolean(busy) ||
                        seat.occupant_type !== "open" ||
                        Boolean(me) ||
                        Boolean(seasonBlockedReason) ||
                        sessionLoading
                      }
                      aria-busy={busy === "claim-seat"}
                      onClick={() =>
                        user
                          ? action("claim-seat", { seat_key: seat.seat_key })
                          : router.push(
                              `/login?next=${encodeURIComponent(`/rooms/${code}/lobby`)}`,
                            )
                      }
                    >
                      <span className="seat-avatar">
                        {seat.occupant_type === "open" ? (
                          <UserPlus size={17} />
                        ) : (
                          seat.display_name.slice(0, 1)
                        )}
                      </span>
                      <span>
                        <strong>{seat.display_name}</strong>
                        <small>
                          {seat.label} ·{" "}
                          {seat.occupant_type === "open"
                            ? busy === "claim-seat"
                              ? "正在认领席位…"
                              : seasonBlockedReason
                                ? "赛季已关闭"
                                : !user
                                  ? "登录或注册后认领"
                                  : "等待认领"
                            : `${seat.is_ready ? "已准备" : "未准备"} · ${seatIsConnected(seat) ? "在线" : "离线"}`}
                        </small>
                      </span>
                      {seat.is_ready && <Check size={16} color="#40df9c" />}
                    </button>
                  ))}
                </div>
              );
            })}
          </div>
        </section>
        <aside className="panel lobby-sidebar">
          <div className="panel-title">
            <h3>邀请与房间信息</h3>
          </div>
          <div className="room-share">
            <strong>{room.code}</strong>
            <span
              className="copy-confirmation"
              role="status"
              aria-live="polite"
            >
              {copied ? "已复制" : ""}
            </span>
            <button
              type="button"
              className={`icon-button ${styles.copyButton}`}
              aria-label={copied ? "邀请链接已复制" : "复制房间邀请链接"}
              onClick={() => void copyInvite()}
            >
              {copied ? <Check /> : <Share2 />}
            </button>
          </div>
          <p className="muted">
            分享六位房间号或链接，其他登录用户即可加入并认领空席。
          </p>
          <div className="lobby-rule">
            <span>房主</span>
            <strong>{room.owner.real_name}</strong>
          </div>
          {room.competition.ranked && (
            <div className="lobby-rule">
              <span>计分赛季</span>
              <strong>{seasonStatusLabel(room.season)}</strong>
            </div>
          )}
          <div className="lobby-rule">
            <span>自动赛程</span>
            <strong>
              {room.current_stage ? room.current_stage.name : "开始后自动运行"}
            </strong>
          </div>
          <div className="lobby-rule">
            <span>空席策略</span>
            <strong>开始时 AI 自动补齐</strong>
          </div>
          {error && (
            <div className="error-box" role="alert">
              {error}
            </div>
          )}
          {unconfirmedAction && (
            <div
              className={connected ? "warning-box" : "error-box"}
              role="alert"
            >
              {connected
                ? "实时连接已恢复，但上一次操作结果尚未确认。请重新执行该操作；系统会沿用同一请求编号安全核对，不会重复提交。"
                : unconfirmedAction.message}
              {connected && (
                <button
                  type="button"
                  className="text-button"
                  onClick={() => setUnconfirmedAction(null)}
                >
                  知道了
                </button>
              )}
            </div>
          )}
          {me && deviceControl === "lost" && (
            <button
              className="button button-secondary"
              disabled={Boolean(busy)}
              onClick={() => void acquireDeviceControl(true)}
            >
              确认接管到当前设备
            </button>
          )}
          {me && deviceControl === "unavailable" && (
            <button
              className="button button-secondary"
              disabled={Boolean(busy)}
              onClick={() => void acquireDeviceControl(false)}
            >
              重试绑定当前设备
            </button>
          )}
          {room.can_control && removableHumans.length > 0 && (
            <details className={styles.secondaryActions}>
              <summary>调整已加入的席位</summary>
              <div
                className="lobby-seat-management"
                aria-label="开赛前席位管理"
              >
                <p className="muted">
                  有人误入或需要换位时，可先移出再重新邀请。
                </p>
                {removableHumans.map((seat) => (
                  <div className="lobby-rule" key={seat.seat_key}>
                    <span>
                      {seat.label} · {seat.display_name}
                    </span>
                    <button
                      type="button"
                      className="button button-small button-secondary"
                      disabled={Boolean(busy) || !deviceCanWrite}
                      onClick={() =>
                        confirm(
                          `确定将 ${seat.display_name} 移出 ${seat.label}？`,
                        ) &&
                        action(`seats/${seat.seat_key}/remove`, {
                          reason: "房主在开赛前调整席位",
                        })
                      }
                    >
                      移出
                    </button>
                  </div>
                ))}
              </div>
            </details>
          )}
          {me && (
            <div className={styles.secondaryActionsList}>
              {me.is_ready && (
                <button
                  className="button button-secondary"
                  disabled={Boolean(busy) || !deviceCanWrite}
                  onClick={() => action("ready", { ready: false })}
                >
                  {room.can_control ? "取消准备并调整" : "取消准备"}
                </button>
              )}
              {!room.can_control && (
                <button
                  className="button button-secondary"
                  disabled={Boolean(busy) || !deviceCanWrite}
                  onClick={() => action("release-seat")}
                >
                  <DoorOpen size={17} />
                  退出当前席位
                </button>
              )}
            </div>
          )}
          {room.can_control && (
            <details
              className={`${styles.secondaryActions} ${styles.dangerDisclosure}`}
            >
              <summary>房间设置</summary>
              <div className={styles.dangerAction}>
                <span>
                  <strong>关闭尚未开始的房间</strong>
                  <small>所有人将退出，操作不可撤销。</small>
                </span>
                <button
                  className="button button-danger"
                  disabled={Boolean(busy) || !deviceCanWrite}
                  onClick={() =>
                    confirm("确定关闭这个尚未开始的房间？") && action("cancel")
                  }
                >
                  <Trash2 size={17} />
                  关闭房间
                </button>
              </div>
            </details>
          )}
          <Link
            className="button button-secondary"
            href={`/rooms/${code}/watch`}
          >
            <Eye size={17} />
            进入观战画面
          </Link>
        </aside>
      </div>
      {me?.occupant_type === "human" && (
        <section
          className="panel lobby-preflight"
          aria-labelledby="microphone-preflight-title"
        >
          <div className="panel-title">
            <div>
              <span className="eyebrow">Device check</span>
              <h2 id="microphone-preflight-title">赛前麦克风检查</h2>
            </div>
            <span className="badge">不影响准备状态</span>
          </div>
          <p className="muted">
            席位与准备状态确认后，建议在首次发言前主动测试。预检只读取实时音量，不录制、不上传，也不是确认准备的硬门禁。
          </p>
          <MicrophonePreflight />
        </section>
      )}
      <nav className={styles.lobbyDock} aria-label="赛前主要操作">
        <div className={styles.dockSummary}>
          <ShieldCheck size={18} aria-hidden="true" />
          <span>
            <strong>{nextStep.title.replace("下一步：", "")}</strong>
            <small id="lobby-primary-reason">{nextStep.detail}</small>
          </span>
        </div>
        <button
          ref={me?.is_ready && room.can_control ? startButton : undefined}
          type="button"
          className={`button ${
            me?.is_ready && room.can_control
              ? "button-primary"
              : me?.is_ready
                ? "button-secondary"
              : me
                ? "button-green"
                : "button-primary"
          }`}
          aria-busy={busy === "ready" || busy === "start"}
          aria-haspopup={
            me?.is_ready && room.can_control ? "dialog" : undefined
          }
          aria-expanded={
            me?.is_ready && room.can_control ? confirmingStart : undefined
          }
          aria-describedby="lobby-primary-reason"
          disabled={primaryDisabled}
          onClick={runPrimaryAction}
        >
          {me?.is_ready && room.can_control ? (
            <Play size={17} />
          ) : me?.is_ready ? (
            <Clock3 size={17} />
          ) : me ? (
            <Check size={17} />
          ) : (
            <UserPlus size={17} />
          )}
          {primaryLabel}
        </button>
      </nav>
      {confirmingStart && (
        <div
          className="stage-confirm-backdrop"
          onMouseDown={(event) => {
            if (event.target !== event.currentTarget || busy) return;
            setConfirmingStart(false);
            requestAnimationFrame(() => startButton.current?.focus());
          }}
        >
          <div
            ref={startConfirmDialog}
            className="stage-confirm-dialog panel"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="lobby-start-confirm-title"
            aria-describedby="lobby-start-confirm-description"
          >
            <strong id="lobby-start-confirm-title">确认开始比赛？</strong>
            <p id="lobby-start-confirm-description">
              系统会锁定当前 {humans.length} 位真人席位，并由 AI 补齐{" "}
              {room.seats.length - humans.length}{" "}
              个空席。确认后所有人会进入比赛舞台；系统依次建立实时语音、播放预设开场提示，再自动进入第一阶段。准备期间不会开始比赛计时，也不需要再次点击开始。
            </p>
            <div className="lobby-start-summary">
              <span>本场辩题</span>
              <strong>{room.topic}</strong>
            </div>
            <div className="card-actions">
              <button
                ref={startConfirmCancel}
                type="button"
                className="button button-small button-secondary"
                disabled={Boolean(busy)}
                onClick={() => {
                  setConfirmingStart(false);
                  requestAnimationFrame(() => startButton.current?.focus());
                }}
              >
                返回检查
              </button>
              <button
                type="button"
                className="button button-small button-green"
                aria-busy={busy === "start"}
                disabled={Boolean(busy)}
                onClick={() => {
                  setConfirmingStart(false);
                  void action("start");
                }}
              >
                {busy === "start" ? "正在开始…" : "确认开始比赛"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
