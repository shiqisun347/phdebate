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
import { apiFetch } from "@/lib/api";
import { LoadError } from "@/components/load-error";
import { MicrophonePreflight } from "@/components/microphone-preflight";
import { competitionDisplayName } from "@/lib/primary-competition";
import { roomCreationBlockedReason, seasonStatusLabel } from "@/lib/seasons";
import { useRoom } from "@/lib/use-room";
import { useSession } from "@/lib/use-session";
import type { Room } from "@/lib/types";

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
  const [busy, setBusy] = useState("");
  const [copied, setCopied] = useState(false);
  const [deviceControl, setDeviceControl] = useState<"unavailable" | "acquiring" | "owned" | "lost">("unavailable");
  const [controlSeq, setControlSeq] = useState<number | null>(null);
  const attempts = useRef<Record<string, { fingerprint: string; key: string }>>(
    {},
  );
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
    if (!room || room.status === "lobby") return;
    if (room.status === "cancelled") router.replace(`/?room_closed=${code}`);
    else if (
      ["completed", "review_required", "terminated"].includes(room.status)
    )
      router.replace(`/rooms/${code}/result`);
    else router.replace(`/rooms/${code}/debate`);
  }, [room, code, router]);
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
      setRoom((current) =>
        !current || data.room.seq >= current.seq ? data.room : current,
      );
      if (path === "start") router.push(`/rooms/${code}/debate`);
      if (path === "cancel") router.push("/");
      return data.room;
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
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
  if (!room) return <div className="loading-screen">正在进入房间…</div>;
  if (room.status !== "lobby")
    return <div className="loading-screen">正在返回当前比赛…</div>;
  const me = room.seats.find((seat) => seat.is_me);
  const deviceCanWrite = !me || deviceControl === "owned";
  const humans = room.seats.filter((seat) => seat.occupant_type === "human");
  const seasonBlockedReason = roomCreationBlockedReason({
    ...room.competition,
    season: room.season,
  });
  return (
    <div className="page-shell">
      <div className="section-head">
        <div>
          <span className="eyebrow">Room Lobby</span>
          <h2>
            比赛房间 <span className="room-code">#{room.code}</span>
          </h2>
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
        <h1>{room.topic}</h1>
        <div className="detail-meta">
          <span>
            <Users size={15} />
            {humans.length} 位真人已加入
          </span>
          <span>
            <Bot size={15} />
            {
              room.seats.filter((seat) => seat.occupant_type === "open").length
            }{" "}
            个空席将由 AI 补齐
          </span>
        </div>
      </div>
      {seasonBlockedReason && (
        <div className="warning-box" style={{ marginBottom: 18 }}>
          <strong>{seasonStatusLabel(room.season)}</strong> ·{" "}
          {seasonBlockedReason} 该房间仍可观战、释放席位或关闭。
        </div>
      )}
      {me?.occupant_type === "human" && (
        <section className="panel" style={{ marginBottom: 18 }} aria-labelledby="microphone-preflight-title">
          <div className="panel-title">
            <div>
              <span className="eyebrow">Device check</span>
              <h2 id="microphone-preflight-title">赛前麦克风检查</h2>
            </div>
            <span className="badge">不影响准备状态</span>
          </div>
          <p className="muted">建议首次发言前主动测试。预检只读取实时音量，不录制、不上传，也不是确认准备的硬门禁。</p>
          <MicrophonePreflight />
        </section>
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
            {(["aff", "neg"] as const).map((side) => (
              <div className={`lobby-team ${side}`} key={side}>
                <h3>{side === "aff" ? "正方" : "反方"}</h3>
                {room.seats
                  .filter((seat) => seat.side === side)
                  .map((seat) => (
                    <button
                      className={`lobby-seat ${seat.occupant_type !== "open" ? "occupied" : ""} ${seat.is_me ? "me" : ""}`}
                      key={seat.seat_key}
                      disabled={
                        seat.occupant_type !== "open" ||
                        Boolean(me) ||
                        Boolean(seasonBlockedReason) ||
                        sessionLoading
                      }
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
                            ? seasonBlockedReason
                              ? "赛季已关闭"
                              : !user
                                ? "登录或注册后认领"
                                : "等待认领"
                            : seat.is_ready
                              ? "已准备"
                              : "未准备"}
                        </small>
                      </span>
                      {seat.is_ready && <Check size={16} color="#40df9c" />}
                    </button>
                  ))}
              </div>
            ))}
          </div>
        </section>
        <aside className="panel lobby-sidebar">
          <div className="panel-title">
            <h3>房间信息</h3>
          </div>
          <div className="room-share">
            <strong>{room.code}</strong>
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
          {me && deviceControl === "lost" && (
            <button
              className="button button-secondary"
              disabled={Boolean(busy)}
              onClick={() => void acquireDeviceControl(true)}
            >
              确认接管到当前设备
            </button>
          )}
          {me ? (
            <>
              <button
                className={`button ${me.is_ready ? "button-secondary" : "button-green"}`}
                disabled={
                  Boolean(busy) ||
                  !deviceCanWrite ||
                  (Boolean(seasonBlockedReason) && !me.is_ready)
                }
                onClick={() => action("ready", { ready: !me.is_ready })}
              >
                {me.is_ready
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
                className="button"
                disabled={
                  Boolean(busy) ||
                  !deviceCanWrite ||
                  Boolean(seasonBlockedReason) ||
                  humans.some((seat) => !seat.is_ready)
                }
                onClick={() => action("start")}
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
    </div>
  );
}
