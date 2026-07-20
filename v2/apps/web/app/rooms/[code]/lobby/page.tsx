"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import {
  Bot,
  Check,
  Clipboard,
  DoorOpen,
  Eye,
  Play,
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
import { roomCreationBlockedReason, seasonStatusLabel } from "@/lib/seasons";
import { useRoom } from "@/lib/use-room";
import { useSession } from "@/lib/use-session";
import type { Room } from "@/lib/types";

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
  if (action.path === "claim-seat") return room.my_seat === action.body.seat_key;
  if (action.path === "release-seat") return !room.my_seat;
  if (action.path === "ready") return Boolean(me?.is_ready);
  if (action.path === "start") return room.status !== "lobby";
  if (action.path === "cancel") return room.status === "cancelled";
  const removedSeat = action.path.match(/^seats\/([^/]+)\/remove$/)?.[1];
  return Boolean(
    removedSeat
    && room.seats.find((seat) => seat.seat_key === removedSeat)?.occupant_type === "open",
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
  const [deviceControl, setDeviceControl] = useState<"unavailable" | "acquiring" | "owned" | "lost">("unavailable");
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
  const mySeatKey = room?.my_seat || "";
  const roomStatus = room?.status || "";
  const controlLease = useMemo(() => {
    if (typeof window === "undefined" || !mySeatKey) return "";
    const key = `jixia-control:${code}:${mySeatKey}`;
    let value = sessionStorage.getItem(key);
    if (!value) {
      value = crypto.randomUUID();
      sessionStorage.setItem(key, value);
    }
    return value;
  }, [code, mySeatKey]);
  const acquireDeviceControl = useCallback(async (force = false) => {
    if (roomStatus !== "lobby" || !mySeatKey || !controlLease) {
      setDeviceControl("unavailable");
      return;
    }
    setDeviceControl("acquiring");
    try {
      const result = await apiFetch<{ seq: number }>(`/api/rooms/${code}/control-lease`, {
        method: "POST",
        headers: { "X-Control-Lease": controlLease },
        body: JSON.stringify({ force }),
      });
      setControlSeq(result.seq);
      setDeviceControl("owned");
      setError("");
    } catch (reason) {
      setDeviceControl("lost");
      setError(reason instanceof Error ? reason.message : "当前设备无法接管辩手席位");
    }
  }, [code, controlLease, mySeatKey, roomStatus]);
  useEffect(() => {
    void acquireDeviceControl(false);
  }, [acquireDeviceControl]);
  useEffect(() => {
    if (!room?.my_seat || deviceControl !== "owned" || controlSeq === null) return;
    const takenOver = room.recent_events.some((event) =>
      event.seq > controlSeq
      && event.type === "seat.control_taken_over"
      && event.payload.seat_key === room.my_seat,
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
        (event) => event.type === "seat.removed_by_owner" && event.payload.seat_key === previous,
      );
      if (removed) setError("房主已在开赛前调整你的席位。你可以重新认领空席，或返回赛事大厅加入其他比赛。");
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
      const focusable = [...startConfirmDialog.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )];
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
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("keydown", closeOnEscape);
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
        else
          delete attempts.current[path];
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
  if (room.status !== "lobby")
    return <PageLoading label="正在返回当前比赛…" />;
  const me = room.seats.find((seat) => seat.is_me);
  const deviceCanWrite = !me || deviceControl === "owned";
  const humans = room.seats.filter((seat) => seat.occupant_type === "human");
  const readyHumans = humans.filter((seat) => seat.is_ready).length;
  const connectedHumans = humans.filter((seat) => seat.connected).length;
  const openSeats = room.seats.filter((seat) => seat.occupant_type === "open").length;
  const removableHumans = humans.filter((seat) => !seat.is_owner && !seat.is_me);
  const seasonBlockedReason = roomCreationBlockedReason({
    ...room.competition,
    season: room.season,
  });
  return (
    <div className="page-shell">
      <div className="section-head">
        <div>
          <span className="eyebrow">Room Lobby</span>
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
      {seasonBlockedReason && (
        <div className="warning-box" style={{ marginBottom: 18 }}>
          <strong>{seasonStatusLabel(room.season)}</strong> ·{" "}
          {seasonBlockedReason} 该房间仍可观战、释放席位或关闭。
        </div>
      )}
      <div className="lobby-layout">
        <section className="panel">
          <div className="panel-title">
            <h2>选择与确认席位</h2>
            <span className="muted">
              {seasonBlockedReason ? "当前赛季不可继续加入" : "点击空席加入"}
            </span>
          </div>
          <div className="lobby-teams">
            {(["aff", "neg"] as const).map((side) => {
              const sideSeats = room.seats.filter((seat) => seat.side === side);
              const sideHumans = sideSeats.filter((seat) => seat.occupant_type === "human").length;
              const sideName = side === "aff" ? "正方" : "反方";
              return (
              <div className={`lobby-team ${side}`} key={side} role="group" aria-label={`${sideName}席位`}>
                <h3>
                  <span>{sideName}</span>
                  <small>{sideHumans} 位真人 · {sideSeats.length} 个席位</small>
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
                      onClick={() => user
                        ? action("claim-seat", { seat_key: seat.seat_key })
                        : router.push(`/login?next=${encodeURIComponent(`/rooms/${code}/lobby`)}`)
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
                            : `${seat.is_ready ? "已准备" : "未准备"} · ${seat.connected ? "在线" : "离线"}`}
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
            <h3>房间信息</h3>
          </div>
          <div className="room-share">
            <strong>{room.code}</strong>
            <span className="copy-confirmation" role="status" aria-live="polite">
              {copied ? "已复制" : ""}
            </span>
            <button
              type="button"
              className="icon-button"
              aria-label={copied ? "邀请链接已复制" : "复制房间邀请链接"}
              onClick={() => void copyInvite()}
            >
              {copied ? <Check /> : <Clipboard />}
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
            <div className={connected ? "warning-box" : "error-box"} role="alert">
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
          {room.can_control && removableHumans.length > 0 && (
            <div className="lobby-seat-management" aria-label="开赛前席位管理">
              <strong>开赛前席位管理</strong>
              <p className="muted">有人误入、长期不准备或需要更换席位时，可先移出再重新邀请。</p>
              {removableHumans.map((seat) => (
                <div className="lobby-rule" key={seat.seat_key}>
                  <span>{seat.label} · {seat.display_name}</span>
                  <button
                    type="button"
                    className="button button-small button-secondary"
                    disabled={Boolean(busy) || !deviceCanWrite}
                    onClick={() => confirm(`确定将 ${seat.display_name} 移出 ${seat.label}？`) && action(`seats/${seat.seat_key}/remove`, { reason: "房主在开赛前调整席位" })}
                  >
                    移出
                  </button>
                </div>
              ))}
            </div>
          )}
          {me ? (
            <>
              <button
                className={`button ${me.is_ready ? "button-secondary" : "button-green"}`}
                aria-busy={busy === "ready"}
                disabled={
                  Boolean(busy) ||
                  !deviceCanWrite ||
                  (Boolean(seasonBlockedReason) && !me.is_ready)
                }
                onClick={() => action("ready", { ready: !me.is_ready })}
              >
                {busy === "ready"
                  ? "正在保存准备状态…"
                  : me.is_ready
                  ? "取消准备"
                  : seasonBlockedReason
                    ? "赛季已关闭"
                    : "确认准备"}
              </button>
              {!room.can_control && (
                <button
                  className="button button-secondary"
                  disabled={Boolean(busy) || !deviceCanWrite}
                  onClick={() => action("release-seat")}
                >
                  <DoorOpen size={17} />
                  释放当前席位
                </button>
              )}
            </>
          ) : (
            <div className="error-box">
              {seasonBlockedReason
                ? "当前赛季不可再认领席位"
                : "认领一个空席后才能参赛"}
            </div>
          )}
          {room.can_control && (
            <>
              <button
                ref={startButton}
                className="button"
                aria-haspopup="dialog"
                aria-expanded={confirmingStart}
                disabled={
                  Boolean(busy) ||
                  !deviceCanWrite ||
                  Boolean(seasonBlockedReason) ||
                  humans.some((seat) => !seat.is_ready)
                }
                onClick={() => setConfirmingStart(true)}
              >
                <Play size={17} />
                {seasonBlockedReason
                  ? "赛季已关闭"
                  : humans.some((seat) => !seat.is_ready)
                    ? "等待全部真人准备"
                    : "锁定席位并开始"}
              </button>
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
            </>
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
        <section className="panel lobby-preflight" aria-labelledby="microphone-preflight-title">
          <div className="panel-title">
            <div>
              <span className="eyebrow">Device check</span>
              <h2 id="microphone-preflight-title">赛前麦克风检查</h2>
            </div>
            <span className="badge">不影响准备状态</span>
          </div>
          <p className="muted">席位与准备状态确认后，建议在首次发言前主动测试。预检只读取实时音量，不录制、不上传，也不是确认准备的硬门禁。</p>
          <MicrophonePreflight />
        </section>
      )}
      {confirmingStart && (
        <div className="stage-confirm-backdrop">
          <div ref={startConfirmDialog} className="stage-confirm-dialog panel" role="alertdialog" aria-modal="true" aria-labelledby="lobby-start-confirm-title" aria-describedby="lobby-start-confirm-description">
            <strong id="lobby-start-confirm-title">确认锁定席位并开始比赛？</strong>
            <p id="lobby-start-confirm-description">开始后将锁定当前 {humans.length} 位真人席位，并由 AI 自动补齐 {room.seats.length - humans.length} 个空席。自动赛程会立即开始，不能再更换辩题或席位。</p>
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
