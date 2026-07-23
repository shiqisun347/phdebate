import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { acquireControlLease, controlLeaseFor } from "@/lib/control-lease";

describe("controlLeaseFor", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("shares one seat lease across tabs through persistent browser storage", () => {
    const first = controlLeaseFor("123456", "aff_1");
    sessionStorage.clear();
    const restoredTab = controlLeaseFor("123456", "aff_1");

    expect(restoredTab).toBe(first);
    expect(localStorage.getItem("jixia-control:123456:aff_1")).toBe(first);
  });

  it("migrates the existing per-tab lease without changing control identity", () => {
    sessionStorage.setItem("jixia-control:123456:aff_1", "legacy-tab-lease");

    expect(controlLeaseFor("123456", "aff_1")).toBe("legacy-tab-lease");
    expect(localStorage.getItem("jixia-control:123456:aff_1")).toBe("legacy-tab-lease");
  });

  it("does not allocate a control identity for a spectator", () => {
    const randomUUID = vi.spyOn(window.crypto, "randomUUID");

    expect(controlLeaseFor("123456", "")).toBe("");
    expect(randomUUID).not.toHaveBeenCalled();
  });

  it("retries a transient room lock without forcing a seat takeover", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        code: "room_busy",
        detail: "房间正在处理其他操作，请稍后重试。",
      }), { status: 409, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        ok: true,
        lease_fingerprint: "same-device",
        seq: 8,
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    const request = acquireControlLease("123456", "same-browser-lease");
    await vi.runAllTimersAsync();

    await expect(request).resolves.toEqual(expect.objectContaining({ seq: 8 }));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    for (const [, init] of fetchMock.mock.calls) {
      expect(init?.body).toBe(JSON.stringify({ force: false }));
    }
  });

  it("does not retry or automatically force a real cross-device conflict", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      detail: "该席位已由另一设备控制，如需切换请确认接管。",
    }), { status: 409, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(acquireControlLease("123456", "different-device")).rejects.toThrow("另一设备控制");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][1]?.body).toBe(JSON.stringify({ force: false }));
  });
});
