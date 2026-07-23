import { chromium } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import path from "node:path";

const competition = {
  id: "competition",
  slug: "daily-4v4",
  name: "4v4 人机辩论正式赛",
  tagline: "",
  description: "",
  rules: "",
  format: "4v4",
  seat_count: 8,
  ranked: false,
  allow_custom_topic: true,
  accent: "blue",
  live_count: 1,
};

const seats = ["aff", "neg"].flatMap((side) =>
  [1, 2, 3, 4].map((position) => ({
    seat_key: `${side}_${position}`,
    side,
    position,
    label: `${side === "aff" ? "正方" : "反方"}${["一", "二", "三", "四"][position - 1]}辩`,
    occupant_type: position === 1 ? "human" : "open",
    display_name: position === 1 ? (side === "aff" ? "林嘉言" : "周亦然") : "待加入",
    is_ready: side === "aff" && position === 1,
    connected: position === 1,
    is_me: side === "aff" && position === 1,
    is_owner: side === "aff" && position === 1,
  })),
);

const lobby = {
  id: "room-id",
  code: "123456",
  topic: "AI 时代，大学生更应该培养提问能力还是编程能力？",
  status: "lobby",
  visibility: "public",
  seq: 3,
  competition,
  season: null,
  owner: { id: "owner", real_name: "林嘉言" },
  seats,
  current_stage: null,
  current_stage_index: -1,
  remaining_seconds: null,
  turn_remaining_seconds: null,
  active_speech: null,
  my_seat: "aff_1",
  can_speak: false,
  speak_reason: "比赛尚未开始",
  can_control: true,
  recent_events: [],
  speeches: [],
};

const readyLobby = {
  ...lobby,
  seq: 4,
  seats: seats.map((seat) =>
    seat.occupant_type === "human" ? { ...seat, is_ready: true } : seat,
  ),
};

const running = {
  ...lobby,
  status: "running",
  seq: 8,
  current_stage: { key: "neg-open", name: "反方一辩立论", kind: "speech", duration: 120, seat: "neg_1" },
  current_stage_index: 2,
  remaining_seconds: 96,
  my_seat: "aff_1",
  can_speak: false,
  speak_reason: "等待反方发言",
  recent_events: [
    { seq: 8, type: "stage.started", payload: { stage_name: "反方一辩立论" }, created_at: new Date().toISOString() },
    { seq: 7, type: "speech.completed", payload: { seat_key: "aff_1" }, created_at: new Date(Date.now() - 20000).toISOString() },
    { seq: 6, type: "stage.started", payload: { stage_name: "正方一辩立论" }, created_at: new Date(Date.now() - 90000).toISOString() },
  ],
};

const debate = {
  ...running,
  can_view_transcript: true,
  active_speech: {
    id: "speech-ai",
    seat_key: "neg_1",
    speaker_type: "ai",
    status: "synthesizing",
    content: "",
    playback_started_at: new Date().toISOString(),
    stream_generation: "visual-generation",
    stream_sample_rate: 24000,
  },
  caption_segments: [{
    segment_id: "caption-ai-1",
    speech_id: "speech-ai",
    text: "提问能力决定我们能否识别真正值得解决的问题。",
    start_ms: 0,
    end_ms: 0,
    is_final: true,
    timing_basis: "agent_text",
    source: "agent",
    updated_at: new Date().toISOString(),
  }],
};

const freeDebate = {
  ...debate,
  seq: 9,
  current_stage: {
    key: "free-1",
    name: "自由辩论",
    kind: "free",
    duration: 240,
    side: "aff",
    selected_human_seat: null,
  },
  active_speech: null,
  caption_segments: [],
  remaining_seconds: 216,
  turn_remaining_seconds: null,
  free_turn_queue: {
    target_side: "aff",
    items: [],
    my_request: null,
    can_request: true,
    request_reason: "轮到正方申请下一轮发言",
    // The component clamps this to its three-second contract; a generous
    // fixture deadline keeps the screenshot deterministic after page compile.
    window_deadline_at: new Date(Date.now() + 60_000).toISOString(),
  },
};

const watch = {
  ...debate,
  my_seat: null,
  can_control: false,
  can_speak: false,
  can_view_transcript: false,
  caption_segments: [],
};

const browser = await chromium.launch({
  headless: true,
  executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
});
const output = path.resolve(import.meta.dirname, "../../../docs/qa/assets/round56-room-control-ux");
await mkdir(output, { recursive: true });
console.log(`visual output: ${output}`);

const viewports = Object.entries({ desktop: { width: 1440, height: 900 }, mobile: { width: 390, height: 844 } });
console.log(`visual viewports: ${viewports.length}`);
for (const [name, viewport] of viewports) {
  console.log(`opening ${name}`);
  const context = await browser.newContext({ viewport });
  for (const [captureName, pageName, room] of [
    ["lobby", "lobby", lobby],
    ["lobby-ready", "lobby", readyLobby],
    ["control", "control", running],
    ["debate", "debate", debate],
    ["debate-free", "debate", freeDebate],
    ["watch", "watch", watch],
  ]) {
    const page = await context.newPage();
    await page.addInitScript(({ projectedRoom }) => {
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
          if (!String(url).includes("/ws/rooms/123456")) {
            return protocols === undefined
              ? new NativeWebSocket(url)
              : new NativeWebSocket(url, protocols);
          }
          queueMicrotask(() => {
            this.readyState = NativeWebSocket.OPEN;
            this.onopen?.(new Event("open"));
            queueMicrotask(() => this.onmessage?.(new MessageEvent("message", {
              data: JSON.stringify({ type: "snapshot", room: projectedRoom }),
            })));
          });
        }
        send() {}
        close(code = 1000, reason = "visual harness close") {
          if (this.readyState === NativeWebSocket.CLOSED) return;
          this.readyState = NativeWebSocket.CLOSED;
          this.onclose?.(new CloseEvent("close", { code, reason, wasClean: true }));
        }
        addEventListener(type, listener) {
          this[`on${type}`] = listener;
        }
        removeEventListener(type, listener) {
          if (this[`on${type}`] === listener) this[`on${type}`] = null;
        }
      }
      window.WebSocket = RoomSnapshotSocket;
    }, { projectedRoom: room });
    page.on("console", (message) => console.log(`[browser:${pageName}] ${message.type()}: ${message.text()}`));
    page.on("pageerror", (error) => console.log(`[browser:${pageName}] pageerror: ${error.message}`));
    await page.route("**/api/auth/session-state", (route) => route.fulfill({ json: { user: { id: "owner", account: "lin", real_name: "林嘉言", role: "user" } } }));
    await page.route("**/api/rooms/123456/control-lease", (route) => route.fulfill({ json: { seq: 3, lease_fingerprint: "visual" } }));
    await page.route("**/api/rooms/123456/rtc-token", (route) => route.fulfill({ json: { enabled: false } }));
    await page.route("**/api/rooms/123456", (route) => route.fulfill({ json: { room } }));
    // Next.js 16 blocks dev HMR resources when the browser origin differs
    // from the server's advertised localhost origin. Keep the visual harness
    // on localhost so hydration (and therefore the mocked room projection)
    // is exercised instead of capturing the server loading shell.
    await page.goto(`http://localhost:3201/rooms/123456/${pageName}`, { waitUntil: "domcontentloaded" });
    await page.locator("h1").waitFor({ timeout: 8_000 });
    if (pageName === "lobby") {
      const connection = await page.locator(".connection").evaluate((element) => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return { text: element.textContent, color: style.color, fontSize: style.fontSize, height: rect.height };
      });
      console.log(`connection ${name}: ${JSON.stringify(connection)}`);
      if (captureName === "lobby-ready") {
        const start = page.getByRole("button", { name: "开始比赛" });
        if (!(await start.isEnabled())) throw new Error("ready owner cannot start the match");
        if (!(await start.evaluate((element) => element.classList.contains("button-primary")))) {
          throw new Error("ready owner start action is not visually primary");
        }
      }
    }
    const dimensions = await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth }));
    if (dimensions.scrollWidth > dimensions.width) throw new Error(`${pageName}-${name} overflows horizontally: ${JSON.stringify(dimensions)}`);
    const dock = page.locator('nav[aria-label="赛前主要操作"], nav[aria-label="比赛主要控制"]');
    if (captureName === "lobby" || captureName === "control") {
      const box = await dock.boundingBox();
      if (!box || box.y + box.height > viewport.height + 1) throw new Error(`${pageName}-${name} dock is outside viewport`);
    } else if (pageName === "debate" && name === "mobile") {
      const trigger = page.getByRole("button", { name: "文字记录" });
      const triggerBox = await trigger.boundingBox();
      const controlsBox = await page.locator(".stage-controls").boundingBox();
      if (!triggerBox || !controlsBox) throw new Error("missing debate mobile controls");
      if (triggerBox.y < controlsBox.y || triggerBox.y + triggerBox.height > controlsBox.y + controlsBox.height) {
        throw new Error(`transcript trigger is outside bottom control safe area: ${JSON.stringify({ triggerBox, controlsBox })}`);
      }
      for (const seat of await page.locator(".stage-seat").all()) {
        const seatBox = await seat.boundingBox();
        if (!seatBox) continue;
        const overlaps = triggerBox.x < seatBox.x + seatBox.width
          && triggerBox.x + triggerBox.width > seatBox.x
          && triggerBox.y < seatBox.y + seatBox.height
          && triggerBox.y + triggerBox.height > seatBox.y;
        if (overlaps) throw new Error(`transcript trigger overlaps a seat: ${JSON.stringify({ triggerBox, seatBox })}`);
      }
      if (captureName === "debate-free") {
        const queueBox = await page.locator("[data-free-turn-queue]").boundingBox();
        if (!queueBox) throw new Error("missing free-debate queue");
        if (queueBox.y < controlsBox.y || queueBox.y + queueBox.height > controlsBox.y + controlsBox.height) {
          throw new Error(`free-debate hand action is outside bottom controls: ${JSON.stringify({ queueBox, controlsBox })}`);
        }
        for (const seat of await page.locator(".stage-seat").all()) {
          const seatBox = await seat.boundingBox();
          if (!seatBox) continue;
          const overlaps = queueBox.x < seatBox.x + seatBox.width
            && queueBox.x + queueBox.width > seatBox.x
            && queueBox.y < seatBox.y + seatBox.height
            && queueBox.y + queueBox.height > seatBox.y;
          if (overlaps) throw new Error(`free-debate queue overlaps a seat: ${JSON.stringify({ queueBox, seatBox })}`);
        }
        const speakBox = await page.locator(".speak-button").boundingBox();
        if (!speakBox) throw new Error("missing free-debate primary speech button");
        const overlapsSpeak = queueBox.x < speakBox.x + speakBox.width
          && queueBox.x + queueBox.width > speakBox.x
          && queueBox.y < speakBox.y + speakBox.height
          && queueBox.y + queueBox.height > speakBox.y;
        if (overlapsSpeak) throw new Error(`free-debate hand action overlaps speech button: ${JSON.stringify({ queueBox, speakBox })}`);
        const hand = page.getByRole("button", { name: "举手申请下一轮" });
        if (!(await hand.isEnabled())) throw new Error("free-debate hand action is not available");
      }
    } else if (pageName === "watch") {
      if (await page.getByRole("button", { name: "文字记录" }).count()) {
        throw new Error("public watch unexpectedly exposes transcript controls");
      }
      if (await page.getByText("提问能力决定我们能否识别真正值得解决的问题。").count()) {
        throw new Error("public watch unexpectedly exposes a live transcript");
      }
    }
    await page.screenshot({ path: path.join(output, `${captureName}-${name}.png`), fullPage: true });
    console.log(`captured ${captureName}-${name}`);
    await page.close();
  }
  await context.close();
}

await browser.close();
