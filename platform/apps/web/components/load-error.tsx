"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";

export function LoadError({ message, retry }: { message: string; retry?: () => void }) {
  const router = useRouter();
  const [roomCode, setRoomCode] = useState("");
  const missingRoom = /房间.*(?:不存在|已被删除)|房间不存在/.test(message);
  const closedRoom = /房间.*(?:已关闭|已取消)|比赛.*(?:已关闭|已取消)/.test(message);
  const permanentRoomError = missingRoom || closedRoom;
  const title = missingRoom
    ? "房间号不存在"
    : closedRoom
      ? "比赛房间已关闭"
      : "页面暂时无法打开";

  function findRoom(event: FormEvent) {
    event.preventDefault();
    if (!/^\d{6}$/.test(roomCode)) return;
    router.push(`/rooms/${roomCode}/lobby`);
  }

  return (
    <div className="loading-screen">
      <div className="load-error-card panel" role="alert">
        <span className="eyebrow">{permanentRoomError ? "Room unavailable" : "Unable to load"}</span>
        <h1>{title}</h1>
        <p>{message}</p>
        {permanentRoomError && (
          <form className="room-recovery-form" onSubmit={findRoom}>
            <label htmlFor="recovery-room-code">查找其他房间</label>
            <div>
              <input
                id="recovery-room-code"
                className="input"
                inputMode="numeric"
                autoComplete="off"
                pattern="\d{6}"
                maxLength={6}
                placeholder="输入六位房间号"
                value={roomCode}
                onChange={(event) => setRoomCode(event.target.value.replace(/\D/g, "").slice(0, 6))}
              />
              <button type="submit" className="button" disabled={roomCode.length !== 6}>进入房间</button>
            </div>
          </form>
        )}
        <div className="hero-actions">
          {retry && !permanentRoomError && <button type="button" className="button" onClick={retry}>重新尝试</button>}
          <Link href="/" className="button button-secondary">返回赛事大厅</Link>
        </div>
      </div>
    </div>
  );
}
