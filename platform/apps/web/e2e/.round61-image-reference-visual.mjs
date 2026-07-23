import { chromium } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import path from "node:path";

const names = {
  aff_1: ["林知夏", "human"],
  aff_2: ["陈述安", "human"],
  aff_3: ["乾元", "ai"],
  aff_4: ["砚舟", "ai"],
  neg_1: ["沈观", "human"],
  neg_2: ["陈思远", "ai"],
  neg_3: ["明夷", "ai"],
  neg_4: ["周亦然", "human"],
};

const competition = {
  id: "competition",
  slug: "daily-4v4",
  name: "4v4 人机辩论正式赛",
  tagline: "",
  description: "",
  rules: "",
  format: "4v4",
  seat_count: 8,
  ranked: true,
  allow_custom_topic: false,
  accent: "blue",
  live_count: 1,
};

const seats = ["aff", "neg"].flatMap((side) =>
  [1, 2, 3, 4].map((position) => {
    const seatKey = `${side}_${position}`;
    const [displayName, occupantType] = names[seatKey];
    return {
      seat_key: seatKey,
      side,
      position,
      label: `${side === "aff" ? "正方" : "反方"}${["一", "二", "三", "四"][position - 1]}辩`,
      occupant_type: occupantType,
      display_name: displayName,
      is_ready: true,
      connected: true,
      is_me: seatKey === "aff_1",
      is_owner: seatKey === "aff_1",
    };
  }),
);

const room = {
  id: "round61-visual-room",
  code: "618466",
  topic: "AI 的迅猛发展提升了还是降低了人类创作者存在的意义？",
  status: "running",
  visibility: "public",
  seq: 26,
  competition,
  season: { id: "season", name: "2026 夏季赛" },
  owner: { id: "owner", real_name: "林知夏" },
  seats,
  current_stage: {
    key: "free-debate",
    name: "自由辩论",
    kind: "free",
    duration: 240,
    side: "aff",
    selected_human_seat: null,
  },
  current_stage_index: 7,
  remaining_seconds: 168,
  turn_remaining_seconds: 24,
  active_speech: {
    id: "speech-ai",
    seat_key: "aff_3",
    speaker_type: "ai",
    status: "playing",
    content: "真正被降低的不是创作者的意义，而是重复劳动在创作中的比例。",
    playback_started_at: new Date().toISOString(),
    stream_generation: "round61-visual-generation",
    stream_sample_rate: 24000,
  },
  my_seat: "aff_1",
  can_speak: false,
  speak_reason: "乾元正在发言",
  can_control: true,
  can_view_transcript: true,
  recent_events: [
    { seq: 26, type: "stage.started", payload: { stage_name: "自由辩论" }, created_at: new Date().toISOString() },
    { seq: 19, type: "stage.started", payload: { stage_name: "反方二辩驳论" }, created_at: new Date(Date.now() - 150000).toISOString() },
    { seq: 13, type: "stage.started", payload: { stage_name: "正方一辩立论" }, created_at: new Date(Date.now() - 300000).toISOString() },
  ],
  speeches: [
    { id: "speech-1", stage_key: "free-debate", status: "completed", seat_key: "aff_1", speaker: "林知夏", content: "技术进步没有替代创作的价值判断，反而让人的选择更加可见。", created_at: new Date(Date.now() - 260000).toISOString() },
    { id: "speech-2", stage_key: "free-debate", status: "completed", seat_key: "neg_1", speaker: "沈观", content: "当表达可以批量生成，个体创作者的稀缺性必然受到挑战。", created_at: new Date(Date.now() - 180000).toISOString() },
  ],
  caption_segments: [{
    segment_id: "caption-ai-1",
    speech_id: "speech-ai",
    text: "真正被降低的不是创作者的意义，而是重复劳动在创作中的比例。",
    start_ms: 0,
    end_ms: 0,
    is_final: true,
    timing_basis: "agent_text",
    source: "agent",
    updated_at: new Date().toISOString(),
  }],
  free_turn_queue: {
    target_side: "aff",
    items: [],
    my_request: null,
    can_request: false,
    request_reason: "当前已有辩手发言",
    window_deadline_at: null,
  },
};

const browser = await chromium.launch({
  headless: true,
  executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
});
const baseUrl = (process.env.E2E_BASE_URL || "http://localhost:3201").replace(/\/$/, "");
const output = path.resolve(
  import.meta.dirname,
  process.env.E2E_OUTPUT_DIR || "../../../docs/qa/round64-image-reference-evidence",
);
await mkdir(output, { recursive: true });

async function installRoomProjection(page, projectedRoom) {
  await page.addInitScript(({ snapshot }) => {
    const NativeWebSocket = window.WebSocket;
    class RoomSnapshotSocket {
      static CONNECTING = NativeWebSocket.CONNECTING;
      static OPEN = NativeWebSocket.OPEN;
      static CLOSING = NativeWebSocket.CLOSING;
      static CLOSED = NativeWebSocket.CLOSED;
      readyState = NativeWebSocket.CONNECTING;
      onopen = null;
      onmessage = null;
      onerror = null;
      onclose = null;
      constructor(url, protocols) {
        if (!String(url).includes("/ws/rooms/618466")) {
          return protocols === undefined ? new NativeWebSocket(url) : new NativeWebSocket(url, protocols);
        }
        queueMicrotask(() => {
          this.readyState = NativeWebSocket.OPEN;
          this.onopen?.(new Event("open"));
          queueMicrotask(() => this.onmessage?.(new MessageEvent("message", {
            data: JSON.stringify({ type: "snapshot", room: snapshot }),
          })));
        });
      }
      send() {}
      close(code = 1000, reason = "visual harness close") {
        if (this.readyState === NativeWebSocket.CLOSED) return;
        this.readyState = NativeWebSocket.CLOSED;
        this.onclose?.(new CloseEvent("close", { code, reason, wasClean: true }));
      }
      addEventListener(type, listener) { this[`on${type}`] = listener; }
      removeEventListener(type, listener) {
        if (this[`on${type}`] === listener) this[`on${type}`] = null;
      }
    }
    window.WebSocket = RoomSnapshotSocket;
  }, { snapshot: projectedRoom });
  await page.route("**/api/auth/session-state", (route) => route.fulfill({ json: { user: { id: "owner", account: "lin", real_name: "林知夏", role: "user" } } }));
  await page.route("**/api/rooms/618466/control-lease", (route) => route.fulfill({ json: { seq: 26, lease_fingerprint: "round61-visual" } }));
  await page.route("**/api/rooms/618466/rtc-token", (route) => route.fulfill({ json: { enabled: false } }));
  await page.route("**/api/rooms/618466", (route) => route.fulfill({ json: { room: projectedRoom } }));
}

async function assertViewport(page, label, viewport) {
  const dimensions = await page.evaluate(() => ({
    width: innerWidth,
    height: innerHeight,
    scrollWidth: document.documentElement.scrollWidth,
    scrollHeight: document.documentElement.scrollHeight,
  }));
  if (dimensions.scrollWidth > dimensions.width + 1) {
    throw new Error(`${label} overflows horizontally: ${JSON.stringify(dimensions)}`);
  }
  const controls = await page.locator(".stage-controls").boundingBox();
  if (!controls || controls.y + controls.height > viewport.height + 1) {
    throw new Error(`${label} controls outside viewport: ${JSON.stringify({ controls, viewport })}`);
  }
  return dimensions;
}

for (const [label, viewport] of Object.entries({ reference: { width: 1672, height: 941 }, mobile: { width: 390, height: 844 } })) {
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  page.on("pageerror", (error) => console.log(`[${label}] pageerror: ${error.message}`));
  await installRoomProjection(page, room);
  await page.goto(`${baseUrl}/rooms/618466/debate`, { waitUntil: "domcontentloaded" });
  await page.getByRole("heading", { name: "自由辩论", exact: true }).waitFor({ timeout: 10_000 });
  await page.locator(".speaker-focus").waitFor({ state: label === "mobile" ? "hidden" : "visible" });
  const dimensions = await assertViewport(page, label, viewport);
  await page.screenshot({ path: path.join(output, `debate-${label}.png`) });
  console.log(`captured debate-${label}: ${JSON.stringify(dimensions)}`);

  if (label === "reference") {
    await page.getByRole("button", { name: "文字记录" }).click();
    await page.getByRole("dialog", { name: "文字记录", exact: true }).waitFor();
    console.log(`transcript geometry: ${JSON.stringify(await page.locator(".stage-transcript-drawer").evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return { rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }, top: style.top, right: style.right, bottom: style.bottom, position: style.position, scrollY, scrollHeight: document.documentElement.scrollHeight };
    }))}`);
    await page.screenshot({ path: path.join(output, "debate-reference-transcript.png") });
    console.log("captured debate-reference-transcript");
  }
  await context.close();
}

const watchContext = await browser.newContext({ viewport: { width: 1672, height: 941 } });
const watchPage = await watchContext.newPage();
const watchRoom = { ...room, my_seat: null, can_control: false, can_speak: false, can_view_transcript: false, caption_segments: [], speeches: [] };
await installRoomProjection(watchPage, watchRoom);
await watchPage.goto(`${baseUrl}/rooms/618466/watch`, { waitUntil: "domcontentloaded" });
await watchPage.getByRole("heading", { name: "自由辩论", exact: true }).waitFor({ timeout: 10_000 });
if (await watchPage.getByRole("button", { name: "文字记录" }).count()) throw new Error("watch page exposes transcript control");
if (await watchPage.getByText("真正被降低的不是创作者的意义").count()) throw new Error("watch page exposes transcript text");
await assertViewport(watchPage, "watch-reference", { width: 1672, height: 941 });
await watchPage.screenshot({ path: path.join(output, "watch-reference.png") });
console.log("captured watch-reference");
await watchContext.close();

await browser.close();
