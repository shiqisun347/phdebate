import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useCountdown, useRoom } from "@/lib/use-room";
import type { Room } from "@/lib/types";

function snapshot(seq: number, code = "123456"): Room {
  return { code, seq } as Room;
}

class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 3;
  static instances: FakeWebSocket[] = [];
  readyState = 0;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  sent: string[] = [];
  closeCalls: { code: number; reason: string }[] = [];
  constructor(public url: string) { FakeWebSocket.instances.push(this); }
  open() { this.readyState = FakeWebSocket.OPEN; this.onopen?.(new Event("open")); }
  message(payload: unknown) { this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent); }
  emitClose(code: number) { this.readyState = FakeWebSocket.CLOSED; this.onclose?.({ code } as CloseEvent); }
  send(value: string) { this.sent.push(value); }
  close(code = 1000, reason = "") {
    this.closeCalls.push({ code, reason });
    this.emitClose(code);
  }
}

function roomResponse(seq: number) {
  return new Response(JSON.stringify({ room: snapshot(seq) }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function mockOnlineState(initial: boolean) {
  let online = initial;
  vi.spyOn(window.navigator, "onLine", "get").mockImplementation(() => online);
  return (value: boolean) => { online = value; };
}

describe("useRoom", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    FakeWebSocket.instances = [];
  });

  it("does not let a slower REST response overwrite a newer WebSocket snapshot", async () => {
    let resolveFetch: ((response: Response) => void) | null = null;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { resolveFetch = resolve; })));
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const { result } = renderHook(() => useRoom("123456"));
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    act(() => {
      FakeWebSocket.instances[0].open();
      FakeWebSocket.instances[0].message({ type: "snapshot", room: snapshot(5) });
    });
    expect(result.current.connected).toBe(true);
    expect(result.current.room?.seq).toBe(5);
    await act(async () => {
      resolveFetch?.(new Response(JSON.stringify({ room: snapshot(2) }), { status: 200, headers: { "Content-Type": "application/json" } }));
      await Promise.resolve();
    });
    expect(result.current.room?.seq).toBe(5);
  });

  it("keeps the authenticated viewer projection when a newer room event wins the initial REST race", async () => {
    let resolveFetch: ((response: Response) => void) | null = null;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { resolveFetch = resolve; })));
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const { result } = renderHook(() => useRoom("123456"));
    const socket = FakeWebSocket.instances[0];
    const wrongBroadcast = {
      ...snapshot(8),
      my_seat: "aff_1",
      can_control: true,
      can_speak: true,
      speak_reason: "轮到你发言",
      seats: [
        { seat_key: "aff_1", is_me: true },
        { seat_key: "neg_1", is_me: false },
      ],
    } as Room;
    act(() => {
      socket.open();
      socket.message({ type: "snapshot", room: wrongBroadcast });
    });
    expect(result.current.room?.my_seat).toBe("aff_1");

    const authenticatedProjection = {
      ...snapshot(7),
      my_seat: "neg_1",
      can_control: false,
      can_speak: false,
      speak_reason: "当前轮到 正方1辩",
      seats: [
        { seat_key: "aff_1", is_me: false },
        { seat_key: "neg_1", is_me: true },
      ],
    } as Room;
    await act(async () => {
      resolveFetch?.(new Response(JSON.stringify({ room: authenticatedProjection }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(result.current.room?.seq).toBe(8);
    expect(result.current.room?.my_seat).toBe("neg_1");
    expect(result.current.room?.can_control).toBe(false);
    expect(result.current.room?.can_speak).toBe(false);
    expect(result.current.room?.speak_reason).toContain("正方1辩");
    expect(result.current.room?.seats.find((seat) => seat.seat_key === "aff_1")?.is_me).toBe(false);
    expect(result.current.room?.seats.find((seat) => seat.seat_key === "neg_1")?.is_me).toBe(true);

    act(() => {
      socket.message({ type: "snapshot", room: { ...wrongBroadcast, seq: 8 } });
    });
    expect(result.current.room?.my_seat).toBe("neg_1");
    expect(result.current.room?.can_speak).toBe(false);
  });

  it("stops every automatic refresh and reconnect after terminal room errors", () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(() => new Promise<Response>(() => undefined));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const { result } = renderHook(() => useRoom("123456"));
    act(() => {
      FakeWebSocket.instances[0].open();
      FakeWebSocket.instances[0].message({ type: "snapshot", room: snapshot(3) });
      FakeWebSocket.instances[0].emitClose(4404);
      vi.advanceTimersByTime(20_000);
    });
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(result.current.connected).toBe(false);
    expect(result.current.error).toContain("不存在");
    act(() => window.dispatchEvent(new Event("online")));
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(result.current.error).toContain("不存在");
    act(() => result.current.reconnect());
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("closes a half-open connection when no server message arrives", () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const { result } = renderHook(() => useRoom("123456"));
    act(() => {
      FakeWebSocket.instances[0].open();
      vi.advanceTimersByTime(60_000);
    });
    expect(FakeWebSocket.instances[0].closeCalls).toContainEqual({ code: 4000, reason: "heartbeat timeout" });
    expect(result.current.error).toContain("响应超时");
  });

  it("pauses reconnects while offline and immediately refreshes and reconnects when online", async () => {
    vi.useFakeTimers();
    const setOnline = mockOnlineState(true);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(roomResponse(1))
      .mockResolvedValueOnce(roomResponse(2));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const { result } = renderHook(() => useRoom("123456"));

    act(() => {
      FakeWebSocket.instances[0].open();
      FakeWebSocket.instances[0].message({ type: "snapshot", room: snapshot(1) });
    });
    expect(result.current.connected).toBe(true);

    act(() => {
      setOnline(false);
      window.dispatchEvent(new Event("offline"));
      vi.advanceTimersByTime(20_000);
    });
    expect(FakeWebSocket.instances[0].closeCalls).toContainEqual({ code: 4001, reason: "browser offline" });
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(result.current.connected).toBe(false);
    expect(result.current.error).toContain("恢复联网后");

    await act(async () => {
      setOnline(true);
      window.dispatchEvent(new Event("online"));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.current.room?.seq).toBe(2);
  });

  it("refreshes but does not duplicate an existing connection on a redundant online event", async () => {
    const setOnline = mockOnlineState(true);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(roomResponse(1))
      .mockResolvedValueOnce(roomResponse(2));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const { result } = renderHook(() => useRoom("123456"));
    act(() => {
      FakeWebSocket.instances[0].open();
      FakeWebSocket.instances[0].message({ type: "snapshot", room: snapshot(1) });
    });

    await act(async () => {
      setOnline(true);
      window.dispatchEvent(new Event("online"));
      await Promise.resolve();
    });
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(result.current.room?.seq).toBe(2));
  });

  it("keeps snapshot request errors separate from a still-broken realtime connection", async () => {
    const setOnline = mockOnlineState(true);
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(roomResponse(2));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const { result } = renderHook(() => useRoom("123456"));
    const first = FakeWebSocket.instances[0];

    act(() => first.onerror?.(new Event("error")));
    await waitFor(() => expect(result.current.snapshotError).toContain("网络连接失败"));
    expect(result.current.connectionError).toContain("实时连接");

    await act(async () => {
      setOnline(true);
      window.dispatchEvent(new Event("online"));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.snapshotError).toBe("");
    expect(result.current.connectionError).toContain("实时连接");
    expect(result.current.connected).toBe(false);

    act(() => {
      first.open();
      first.message({ type: "snapshot", room: snapshot(3) });
    });
    expect(result.current.connectionError).toBe("");
    expect(result.current.error).toBe("");
    expect(result.current.connected).toBe(true);
  });

  it("manual reconnect replaces the current socket without leaving a retry timer", () => {
    vi.useFakeTimers();
    mockOnlineState(true);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(roomResponse(1)));
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const { result } = renderHook(() => useRoom("123456"));
    const first = FakeWebSocket.instances[0];

    act(() => result.current.reconnect());
    expect(first.closeCalls).toContainEqual({ code: 1000, reason: "hook cleanup" });
    expect(FakeWebSocket.instances).toHaveLength(2);
    act(() => vi.advanceTimersByTime(20_000));
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it("never exposes the previous room or its live event while switching room codes", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const { result, rerender } = renderHook(({ code }: { code: string }) => useRoom(code), {
      initialProps: { code: "123456" },
    });

    act(() => {
      FakeWebSocket.instances[0].open();
      FakeWebSocket.instances[0].message({
        type: "snapshot",
        room: snapshot(4),
        event: { type: "stage.advanced", room_code: "123456", seq: 4 },
      });
    });
    expect(result.current.room?.code).toBe("123456");
    expect(result.current.liveEvent?.room_code).toBe("123456");

    rerender({ code: "654321" });

    expect(result.current.room).toBeNull();
    expect(result.current.liveEvent).toBeNull();
    expect(result.current.connected).toBe(false);
    expect(FakeWebSocket.instances[0].closeCalls).toContainEqual({ code: 1000, reason: "hook cleanup" });
    expect(FakeWebSocket.instances).toHaveLength(2);

    act(() => {
      // A late callback retained by a browser implementation must not restore
      // the old room after its socket has been replaced.
      FakeWebSocket.instances[0].message({
        type: "snapshot",
        room: snapshot(9),
        event: { type: "speech.started", room_code: "123456", seq: 9 },
      });
    });
    expect(result.current.room).toBeNull();
    expect(result.current.liveEvent).toBeNull();
  });

  it("rejects a cross-room payload instead of rendering its snapshot or live event", () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const { result } = renderHook(() => useRoom("123456"));
    const socket = FakeWebSocket.instances[0];

    act(() => {
      socket.open();
      socket.message({ type: "snapshot", room: snapshot(2) });
      socket.message({
        type: "snapshot",
        room: snapshot(99, "654321"),
        event: { type: "stage.advanced", room_code: "654321", seq: 99 },
      });
    });

    expect(result.current.room?.code).toBe("123456");
    expect(result.current.room?.seq).toBe(2);
    expect(result.current.liveEvent).toBeNull();
    expect(result.current.error).toContain("房间不匹配");
    expect(socket.closeCalls).toContainEqual({ code: 4002, reason: "room mismatch" });
  });

  it("removes online listeners, timers, and the socket on unmount", async () => {
    vi.useFakeTimers();
    const setOnline = mockOnlineState(true);
    let resolveFetch: ((response: Response) => void) | null = null;
    const fetchMock = vi.fn(() => new Promise<Response>((resolve) => { resolveFetch = resolve; }));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const { unmount } = renderHook(() => useRoom("123456"));
    const socket = FakeWebSocket.instances[0];
    act(() => socket.open());
    expect(vi.getTimerCount()).toBeGreaterThan(0);

    unmount();
    expect(socket.closeCalls).toContainEqual({ code: 1000, reason: "hook cleanup" });
    expect(vi.getTimerCount()).toBe(0);
    act(() => {
      setOnline(false);
      window.dispatchEvent(new Event("offline"));
      setOnline(true);
      window.dispatchEvent(new Event("online"));
      vi.advanceTimersByTime(20_000);
    });
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolveFetch?.(roomResponse(9));
      await Promise.resolve();
    });
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});

describe("useCountdown", () => {
  afterEach(() => vi.useRealTimers());

  it("counts down locally and resynchronizes on a newer room sequence", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(
      ({ initial, seq }: { initial: number | null; seq: number }) => useCountdown(initial, seq),
      { initialProps: { initial: 5, seq: 1 } },
    );

    act(() => vi.advanceTimersByTime(2100));
    expect(result.current).toBe(3);

    rerender({ initial: 10, seq: 2 });
    expect(result.current).toBe(10);
  });

  it("never becomes negative", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useCountdown(1, 1));
    act(() => vi.advanceTimersByTime(5000));
    expect(result.current).toBe(0);
  });

  it("freezes while paused and resumes from the authoritative value", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(
      ({ initial, seq, active }: { initial: number; seq: number; active: boolean }) => useCountdown(initial, seq, active),
      { initialProps: { initial: 30, seq: 1, active: true } },
    );

    act(() => vi.advanceTimersByTime(2100));
    expect(result.current).toBe(28);
    rerender({ initial: 28, seq: 2, active: false });
    act(() => vi.advanceTimersByTime(5000));
    expect(result.current).toBe(28);

    rerender({ initial: 28, seq: 3, active: true });
    act(() => vi.advanceTimersByTime(1100));
    expect(result.current).toBe(27);
  });

  it("uses wall-clock time after a background-tab delay", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useCountdown(60, 1, true));
    act(() => {
      vi.setSystemTime(Date.now() + 35_000);
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(result.current).toBe(25);
  });
});
