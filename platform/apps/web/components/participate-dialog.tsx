"use client";

import { DoorOpen, Plus, Search, X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { ApiRequestError, apiFetch } from "@/lib/api";
import { competitionDisplayName, isPrimaryCompetition } from "@/lib/primary-competition";
import { roomCreationBlockedReason, seasonStatusLabel } from "@/lib/seasons";
import { useSession } from "@/lib/use-session";
import type { Competition, Room } from "@/lib/types";

function roomLookupError(error: unknown, roomCode: string): string {
  if (error instanceof ApiRequestError && error.status === 404) {
    return `没有找到房间 #${roomCode}。请核对房间号，或向房主确认比赛是否已关闭。`;
  }
  return error instanceof Error ? error.message : "房间查询失败，请稍后重试。";
}

export function ParticipateDialog({ competition, onClose }: { competition: Competition; onClose: () => void }) {
  const router = useRouter();
  const { user, loading: sessionLoading } = useSession();
  const [mode, setMode] = useState<"create" | "join">(roomCreationBlockedReason(competition) ? "join" : "create");
  const [detail, setDetail] = useState<Competition>(competition);
  const [seat, setSeat] = useState("");
  const [topicId, setTopicId] = useState("");
  const [customTopic, setCustomTopic] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const createAttempt = useRef<{ fingerprint: string; key: string } | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const joinInputRef = useRef<HTMLInputElement | null>(null);
  const activeRoomCode = error.match(/房间 #(\d{6})/)?.[1];
  const normalizedCustomTopic = customTopic.trim().replace(/\s+/g, " ");
  const customTopicInvalid = normalizedCustomTopic.length > 0 && normalizedCustomTopic.length < 4;
  const creationBlockedReason = roomCreationBlockedReason(detail);
  const hasManagedTopics = Boolean(detail.topics?.length);
  const topicUnavailable = !detail.allow_custom_topic && !hasManagedTopics;
  const seats = useMemo(() => {
    const perSide = detail.seat_count / 2;
    return ["aff", "neg"].flatMap((side) => Array.from({ length: perSide }, (_, index) => ({ key: `${side}_${index + 1}`, label: `${side === "aff" ? "正方" : "反方"}${index + 1}辩` })));
  }, [detail.seat_count]);
  useEffect(() => {
    void apiFetch<{ competition: Competition }>(`/api/competitions/${competition.slug}`)
      .then((data) => {
        setDetail(data.competition);
        if (data.competition.topics?.[0]) setTopicId(data.competition.topics[0].id);
        if (roomCreationBlockedReason(data.competition)) setMode("join");
      })
      .catch((err) => setError(err instanceof Error ? err.message : "赛事设置载入失败"));
  }, [competition.slug]);

  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousBodyOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeButtonRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = [...dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
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
    const onFocusIn = (event: FocusEvent) => {
      if (!dialogRef.current || dialogRef.current.contains(event.target as Node)) return;
      const firstFocusable = dialogRef.current.querySelector<HTMLElement>(
        'button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      (firstFocusable || closeButtonRef.current)?.focus();
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("focusin", onFocusIn);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("focusin", onFocusIn);
      document.body.style.overflow = previousBodyOverflow;
      previousFocus?.focus();
    };
  }, [onClose]);

  useEffect(() => {
    if (mode !== "join") return;
    const frame = window.requestAnimationFrame(() => joinInputRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [mode]);

  function destinationForRoom(room: Room): "lobby" | "debate" | "watch" | "result" {
    if (room.status === "lobby") return "lobby";
    if (["completed", "review_required", "terminated"].includes(room.status)) return "result";
    return room.my_seat ? "debate" : "watch";
  }

  function continueToLogin(next: string) {
    router.push(`/login?next=${encodeURIComponent(next)}`);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const normalized = code.replace(/\D/g, "").slice(0, 6);
    if (!sessionLoading && !user) {
      if (mode === "create" || normalized.length !== 6) {
        continueToLogin(`/?participate=${encodeURIComponent(detail.slug)}`);
        return;
      }
      setBusy(true);
      setError("");
      try {
        const found = await apiFetch<{ room: Room }>(`/api/rooms/${normalized}`);
        if (found.room.status === "cancelled") {
          throw new Error("该房间已取消，请输入其他房间号。");
        }
        const destination = destinationForRoom(found.room);
        if (destination === "lobby") continueToLogin(`/rooms/${normalized}/lobby`);
        else router.push(`/rooms/${normalized}/${destination}`);
      } catch (err) {
        if (err instanceof ApiRequestError && [401, 403].includes(err.status)) {
          continueToLogin(`/rooms/${normalized}/lobby`);
        } else {
          setError(roomLookupError(err, normalized));
        }
      } finally {
        setBusy(false);
      }
      return;
    }
    setBusy(true); setError("");
    try {
      if (mode === "join") {
        const found = await apiFetch<{ room: Room }>(`/api/rooms/search?code=${normalized}`);
        if (found.room.status === "cancelled") {
          throw new Error("该房间已取消，请输入其他房间号。");
        }
        const destination = destinationForRoom(found.room);
        router.push(`/rooms/${normalized}/${destination}`);
      } else {
        if (creationBlockedReason) throw new Error(creationBlockedReason);
        if (topicUnavailable) throw new Error("当前赛事暂无可用辩题，请联系管理员补充题库。");
        const body = {
          competition_slug: detail.slug,
          topic_id: normalizedCustomTopic ? null : topicId || null,
          custom_topic: normalizedCustomTopic || null,
          seat_key: seat,
          visibility: "public",
        };
        const fingerprint = JSON.stringify(body);
        if (!createAttempt.current || createAttempt.current.fingerprint !== fingerprint) {
          createAttempt.current = { fingerprint, key: crypto.randomUUID() };
        }
        const data = await apiFetch<{ room: Room }>("/api/rooms", {
          method: "POST",
          headers: { "X-Idempotency-Key": createAttempt.current.key },
          body: JSON.stringify(body)
        });
        createAttempt.current = null;
        router.push(`/rooms/${data.room.code}/lobby`);
      }
    } catch (err) {
      const message = mode === "join"
        ? roomLookupError(err, code.replace(/\D/g, "").slice(0, 6))
        : err instanceof Error ? err.message : "操作失败";
      if (message.includes("登录")) {
        const normalized = code.replace(/\D/g, "").slice(0, 6);
        const next = mode === "join" && normalized.length === 6
          ? `/rooms/${normalized}/lobby`
          : `/?participate=${encodeURIComponent(detail.slug)}`;
        continueToLogin(next);
      }
      else setError(message);
    } finally { setBusy(false); }
  }
  return (
    <div className="dialog-overlay" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div ref={dialogRef} className="dialog participate-dialog" role="dialog" aria-modal="true" aria-labelledby="participate-title" aria-describedby="participate-description">
        <div className="dialog-head"><div><span className="eyebrow">创建或加入比赛</span><h2 id="participate-title">{competitionDisplayName(detail)}</h2></div><button ref={closeButtonRef} type="button" className="icon-button" aria-label="关闭参赛窗口" onClick={onClose}><X /></button></div>
        <p id="participate-description" className="sr-only">创建新比赛房间，或输入六位房间号返回已有比赛。</p>
        <div className="choice-grid">
          <button type="button" aria-pressed={mode === "create"} disabled={busy || Boolean(creationBlockedReason)} className={`choice-card ${mode === "create" ? "active" : ""}`} onClick={() => setMode("create")}><Plus size={20} /><strong>{isPrimaryCompetition(detail) ? "创建 4v4 比赛" : "创建比赛"}</strong><small>{creationBlockedReason ? "当前赛季不可创建" : "选择席位并邀请队友"}</small></button>
          <button type="button" aria-pressed={mode === "join"} disabled={busy} className={`choice-card ${mode === "join" ? "active" : ""}`} onClick={() => setMode("join")}><Search size={20} /><strong>搜索房间</strong><small>输入六位比赛房间号</small></button>
        </div>
        {detail.ranked && (
          <div className={creationBlockedReason ? "warning-box" : "success-box"} style={{ marginTop: 16 }}>
            <strong>{seasonStatusLabel(detail.season)}</strong>
            {creationBlockedReason && <span> · {creationBlockedReason} 仍可通过房间号进入已有比赛。</span>}
          </div>
        )}
        <form className="form-stack" onSubmit={submit} style={{ marginTop: 20 }}>
          {mode === "join" ? (
            <div className="field"><label htmlFor="join-room-code">六位房间号</label><input ref={joinInputRef} id="join-room-code" className="input" inputMode="numeric" placeholder="例如 381526" value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, "").slice(0, 6))} required pattern="\d{6}" /></div>
          ) : sessionLoading ? (
            <div className="success-box" role="status"><strong>正在确认登录状态…</strong><span>确认完成后再显示与你身份匹配的参赛设置。</span></div>
          ) : !user ? (
            <div className="success-box"><strong>先完成登录或注册</strong><span>认证后会自动回到当前赛事，再选择辩题和席位创建比赛。</span></div>
          ) : (
            <>
              {hasManagedTopics && <div className="field"><label htmlFor="competition-topic">题库辩题</label><select id="competition-topic" className="select" value={topicId} disabled={Boolean(normalizedCustomTopic)} onChange={(event) => setTopicId(event.target.value)}>{detail.topics?.map((topic) => <option key={topic.id} value={topic.id}>{topic.title}</option>)}</select>{normalizedCustomTopic && <small className="muted">已填写自定义辩题，本场将优先使用自定义内容。</small>}</div>}
              {detail.allow_custom_topic ? <div className="field"><label htmlFor="custom-debate-topic">自定义辩题{hasManagedTopics ? "（可选）" : ""}</label><textarea id="custom-debate-topic" className="textarea" minLength={4} maxLength={300} value={customTopic} onChange={(event) => setCustomTopic(event.target.value)} placeholder={hasManagedTopics ? "留空则使用上方题库辩题" : "输入训练辩题"} /><small className={customTopicInvalid?"field-error":"muted"}>{customTopicInvalid?"自定义辩题至少需要 4 个字符。":`${normalizedCustomTopic.length}/300`}</small></div> : !hasManagedTopics && <div className="field"><label htmlFor="competition-topic">本场辩题</label><select id="competition-topic" className="select" value="" disabled><option value="">暂无可用辩题</option></select><small className="field-error">请联系管理员补充该赛事题库。</small></div>}
              <fieldset className="field seat-fieldset">
                <legend>选择你的人类辩手席位</legend>
                <div className="seat-picker">
                  {seats.map((item) => (
                    <label
                      key={item.key}
                      className={`seat-option ${seat === item.key ? "active" : ""}`}
                    >
                      <input
                        className="sr-only"
                        type="radio"
                        name="human-seat"
                        value={item.key}
                        checked={seat === item.key}
                        onChange={() => setSeat(item.key)}
                        aria-describedby="seat-selection-summary"
                      />
                      <span>{item.label}</span>
                    </label>
                  ))}
                </div>
                <small id="seat-selection-summary" className={seat ? "success-text" : "muted"} role="status" aria-live="polite">
                  {seat ? `你将作为：${seats.find((item) => item.key === seat)?.label}` : "请选择一个席位后再创建比赛。"}
                </small>
              </fieldset>
              <p className="muted" style={{ fontSize: 12, margin: 0 }}>其他玩家可通过房间号认领空席；开始比赛时，剩余席位由 AI 自动填充。</p>
            </>
          )}
          {error && <div className="error-box" role="alert">{error}{activeRoomCode && <Link className="button button-small button-secondary" href={`/rooms/${activeRoomCode}/lobby`} onClick={onClose}>返回当前比赛</Link>}</div>}
          <button className="button" disabled={sessionLoading || busy || customTopicInvalid || (mode === "create" && Boolean(user) && !seat) || (mode === "create" && (Boolean(creationBlockedReason) || topicUnavailable)) || (mode === "join" && code.length !== 6)}>{mode === "create" ? <><DoorOpen size={18} />{sessionLoading ? "正在确认登录状态…" : !user ? "登录或注册后创建" : busy ? "正在创建…" : topicUnavailable ? "暂无可用辩题" : creationBlockedReason ? "赛季未开放" : !seat ? "请先选择席位" : "创建比赛"}</> : <>{sessionLoading ? "正在确认登录状态…" : !user ? "登录或注册后进入" : busy ? "正在搜索…" : "进入房间"}</>}</button>
        </form>
      </div>
    </div>
  );
}
