import { apiFetch, ApiRequestError } from "@/lib/api";

const CONTROL_LEASE_PREFIX = "jixia-control";
const memoryLeases = new Map<string, string>();
const TRANSIENT_RETRY_DELAYS_MS = [120, 320];

function storageKey(roomCode: string, seatKey: string) {
  return `${CONTROL_LEASE_PREFIX}:${roomCode}:${seatKey}`;
}

function usableLease(value: string | null): value is string {
  return Boolean(value && value.length <= 64);
}

function read(storage: Storage, key: string) {
  try {
    const value = storage.getItem(key);
    return usableLease(value) ? value : "";
  } catch {
    return "";
  }
}

function write(storage: Storage, key: string, value: string) {
  try {
    storage.setItem(key, value);
  } catch {
    // Privacy modes can disable either storage implementation. The in-memory
    // copy below still keeps the lease stable for this mounted application.
  }
}

/**
 * Return the stable control identity for one browser device and one seat.
 *
 * localStorage deliberately represents the browser installation rather than
 * a single tab. This prevents a lobby -> stage navigation, a restored tab, or
 * a second tab on the same device from looking like an unrelated device. The
 * old sessionStorage value is migrated so existing rooms keep their lease.
 */
export function controlLeaseFor(roomCode: string, seatKey: string) {
  if (typeof window === "undefined" || !roomCode || !seatKey) return "";
  const key = storageKey(roomCode, seatKey);
  const persistent = read(window.localStorage, key);
  const legacy = read(window.sessionStorage, key);
  const remembered = memoryLeases.get(key) || "";
  const value = persistent || legacy || remembered || window.crypto.randomUUID();

  memoryLeases.set(key, value);
  write(window.localStorage, key, value);
  write(window.sessionStorage, key, value);
  return value;
}

export function isControlLeaseConflict(error: unknown) {
  if (!(error instanceof ApiRequestError) || error.status !== 409) return false;
  return [
    "另一设备控制",
    "另一设备发言",
    "其他设备上接管",
    "其他设备接管",
    "其他设备控制",
  ].some((message) => error.message.includes(message));
}

export function isTransientControlLeaseError(error: unknown) {
  if (!(error instanceof ApiRequestError)) return false;
  if (error.status === 0 || error.status === 503) return true;
  return error.status === 409 && [
    "房间正在处理其他操作",
    "房间操作等待超时",
  ].some((message) => error.message.includes(message));
}

function wait(milliseconds: number) {
  return new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds));
}

/**
 * Bind the current browser to its seat without turning a short database lock
 * race into a false cross-device takeover. Real lease conflicts are never
 * retried or forced automatically.
 */
export async function acquireControlLease(roomCode: string, lease: string, force = false) {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await apiFetch<{ lease_fingerprint: string; seq: number }>(
        `/api/rooms/${roomCode}/control-lease`,
        {
          method: "POST",
          headers: { "X-Control-Lease": lease },
          body: JSON.stringify({ force }),
        },
      );
    } catch (error) {
      const delay = TRANSIENT_RETRY_DELAYS_MS[attempt];
      if (delay === undefined || !isTransientControlLeaseError(error)) throw error;
      await wait(delay);
    }
  }
}
