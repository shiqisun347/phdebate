#!/usr/bin/env node

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { chromium } from "playwright-core";

const frontendUrl = (
  process.env.PHDEBATE_BROWSER_BASE_URL ||
  process.env.PHDEBATE_FRONTEND_URL ||
  "http://127.0.0.1:5174"
).replace(/\/+$/, "");
const apiUrl = (
  process.env.PHDEBATE_BROWSER_API_URL ||
  process.env.PHDEBATE_BASE_URL ||
  (frontendUrl.includes(":5174") ? "http://127.0.0.1:8000" : frontendUrl)
).replace(/\/+$/, "");
const matchId = process.env.PHDEBATE_BROWSER_MATCH_ID || process.env.PHDEBATE_SMOKE_MATCH_ID || "current";
const speakerId = process.env.PHDEBATE_BROWSER_SPEAKER_ID || "spk_aff_3";
const token =
  process.env.PHDEBATE_BROWSER_SMOKE_TOKEN ||
  process.env.PHDEBATE_SMOKE_TOKEN ||
  process.env.PHDEBATE_ADMIN_PASSWORD ||
  process.env.PHDEBATE_HOST_PASSWORD ||
  "";
const resetDemo = process.env.PHDEBATE_BROWSER_RESET !== "0";
const headless = process.env.PHDEBATE_BROWSER_HEADLESS !== "0";
const audioSmoke = process.env.PHDEBATE_BROWSER_AUDIO_SMOKE === "1";
const results = [];

async function main() {
  const executablePath = process.env.PHDEBATE_BROWSER_EXECUTABLE || findChromeExecutable();
  const fakeAudioPath = audioSmoke ? process.env.PHDEBATE_BROWSER_FAKE_AUDIO || writeFakeAudioFile() : "";
  const homeDir = browserHomeDir();
  const browser = await chromium.launch({
    headless,
    args: stableChromeArgs(audioSmoke, fakeAudioPath, homeDir),
    env: { ...process.env, HOME: homeDir },
    ...(executablePath ? { executablePath } : {})
  }).catch((error) => {
    throw new Error(
      `无法启动浏览器：${error.message}\n` +
      "可设置 PHDEBATE_BROWSER_EXECUTABLE 指向 Chrome/Chromium，或安装 Playwright 浏览器后重试。"
    );
  });

  const context = await browser.newContext({
    viewport: { width: 1440, height: 960 },
    ignoreHTTPSErrors: true,
    permissions: audioSmoke ? ["microphone"] : []
  });

  try {
    await step("API health", async () => {
      const data = await apiRequest("GET", "/api/health", undefined, { auth: false });
      assert(data.ok === true, "API health should be ok");
    });

    if (resetDemo) {
      await step("Reset demo state", async () => {
        const data = await apiRequest("POST", "/api/demo/reset");
        assert(Boolean(data.data.match.id), "demo reset should return a match id");
        const ready = await apiRequest("POST", "/api/matches/current/reset", { confirm_text: "重置比赛" });
        assert(ready.data.match.status === "ready", "formal reset should prepare a ready match");
        assert(ready.data.match.screen_scene === "idle", "formal reset should return screen to idle");
      });
    }

    await step("Open navigation page", async () => {
      const page = await trackedPage(context, "navigation");
      await page.goto(pageUrl("/", { token }), { waitUntil: "domcontentloaded" });
      await expectText(page, "现场导航");
      await expectText(page, "当前比赛");
      await expectText(page, "中科院计算所第一届人机辩论赛");
      await expectText(page, "AI 时代，我们更应该培养编程思维 / 提问思维");
      await expectText(page, "技术后台");
      await expectText(page, "大屏");
      await expectText(page, "辩手端");
    });

    await step("Open host console", async () => {
      const page = await trackedPage(context, "host");
      await apiRequest("POST", `/api/matches/${matchId}/reset`, { confirm_text: "重置比赛" });
      await page.goto(pageUrl("/host", { token }), { waitUntil: "domcontentloaded" });
      await expectText(page, "主持导播台");
      await expectText(page, "当前环节");
      await expectText(page, "发言权限");
      await expectText(page, "下一步建议");
      await expectText(page, "赛后流程");
      await expectText(page, "WS 已连接");
      await page.waitForTimeout(1200);
      assert(await page.getByText("切换发言人", { exact: false }).count() === 0, "host should not expose speaker activation");
      assert(await page.getByRole("button", { name: "开始发言", exact: true }).count() === 0, "host should not expose speaker start controls");
      await page.locator(".host-primary-actions").getByRole("button", { name: "开始", exact: true }).click();
      await expectText(page, "进行中");
      assert(await page.getByRole("button", { name: "关闭学生投票" }).count() === 0, "host should not expose manual audience vote close");
      assert(await page.getByRole("button", { name: /自由辩论/ }).count() === 0, "host phase list should be read-only");
    });

    await step("Open technical admin and run speech diagnostics", async () => {
      const page = await trackedPage(context, "admin");
      await page.goto(pageUrl("/admin", { token }), { waitUntil: "domcontentloaded" });
      await expectText(page, "人机辩论赛");
      await expectText(page, "控制台 Admin");
      await expectText(page, "概览");
      const nav = page.getByRole("navigation");
      await nav.getByRole("button", { name: /比赛管理/ }).click();
      await expectText(page, "管理所有比赛");
      await expectText(page, "新建比赛");
      await nav.getByRole("button", { name: /调试与总览/ }).click();
      await expectText(page, "赛前设备与功能自检");
      await page.getByRole("button", { name: /重新自检/ }).click();
      await expectText(page, /通过|警告|失败/);
      await nav.getByRole("button", { name: /控场台/ }).click();
      await expectText(page, "人工辩手状态与开麦");
      await expectText(page, "AI 辩手控制");
      await expectText(page, "结束当前人工发言");
      await nav.getByRole("button", { name: /辩手管理/ }).click();
      await expectText(page, "按当前赛制确定辩手数量与席位");
      await nav.getByRole("button", { name: /Agent 管理/ }).click();
      await expectText(page, "配置 AI 辩手的接入方式");
      await expectText(page, "新增 Agent");
      await nav.getByRole("button", { name: /语音引擎/ }).click();
      await expectText(page, "ASR · 语音识别");
      await expectText(page, "TTS · 语音合成");
      await expectText(page, "TTS 流式合成与播放测试");
      await nav.getByRole("button", { name: /数据管理/ }).click();
      await expectText(page, "当前比赛数据统计");
      await expectText(page, "历史归档与导出");
    });

    await step("Open screen live page", async () => {
      await apiRequest("POST", `/api/matches/${matchId}/reset`, { confirm_text: "重置比赛" });
      await apiRequest("POST", `/api/matches/${matchId}/resume`);
      await apiRequest("POST", `/api/matches/${matchId}/phases/phase_free_debate/start`);
      await apiRequest("POST", `/api/matches/${matchId}/screen/scene`, { scene: "live", live_mode: "free" });
      const page = await trackedPage(context, "screen");
      await page.goto(pageUrl("/screen", { token }), { waitUntil: "domcontentloaded" });
      await expectText(page, "自由辩论");
      await expectText(page, "正方剩余");
      await expectText(page, "反方剩余");
    });

    await step(audioSmoke ? "Open speaker console and record PCM archive" : "Open speaker console", async () => {
      const page = await trackedPage(context, "console");
      await page.goto(pageUrl("/console", { token }), { waitUntil: "domcontentloaded" });
      await expectText(page, /身份选择|当前阶段/);
      if (await page.getByText("身份选择", { exact: false }).first().isVisible({ timeout: 1000 }).catch(() => false)) {
        await expectText(page, "身份选择");
        await expectText(page, "硬件测试");
        await page.getByText("正方三辩", { exact: false }).first().click();
        await page.getByRole("button", { name: /下一步/ }).click();
        await expectText(page, "麦克风");
        await expectText(page, "HTTP 访问时，浏览器可能不开放麦克风权限");
        assert(await page.getByText("扬声器", { exact: false }).count() === 0, "agent hardware test should not require local speaker output");
        return;
      }
      await expectText(page, "当前阶段");
      await expectText(page, /开始发言|结束发言|可以发言|尚未轮到你|等待比赛/);
      if (!audioSmoke) return;
      const startButton = page.getByRole("button", { name: /开始发言/ });
      if (await startButton.isVisible({ timeout: 1000 }).catch(() => false)) {
        await startButton.click();
      }
      await expectText(page, /PCM\/L16 归档中|已归档|webm 归档中/, 15000);
      const snapshot = await waitForSnapshot((state) => {
        const asset = state.audio_assets.find((item) => item.speaker_id === speakerId);
        return Boolean(asset && asset.chunk_count > 0);
      }, 12000);
      const asset = snapshot.audio_assets.find((item) => item.speaker_id === speakerId);
      assert(asset, "speaker audio asset should exist");
      assert(asset.mime_type.toLowerCase().includes("l16"), `expected PCM/L16 archive, got ${asset.mime_type}`);
      await apiRequest("POST", `/api/matches/${matchId}/speakers/${speakerId}/stop-speaking`);
      await waitForSnapshot((state) => {
        const completed = state.audio_assets.find((item) => item.speech_id === asset.speech_id);
        return completed?.status === "completed";
      }, 12000);
    });

    await step("Open human speaker ready console", async () => {
      const page = await trackedPage(context, "console-human");
      await setConsoleReady(page, "spk_aff_3");
      await page.goto(pageUrl("/console/spk_aff_3", { token }), { waitUntil: "domcontentloaded" });
      await expectText(page, "当前阶段");
      await expectText(page, /开始发言|暂停发言|继续发言|结束发言|尚未轮到你|等待比赛/);
      assert(await page.getByText("AI 辩手状态", { exact: false }).count() === 0, "human console should not show agent status panel");
    });

    await step("Open AI speaker ready console", async () => {
      await apiRequest("POST", `/api/matches/${matchId}/resume`);
      await apiRequest("POST", `/api/matches/${matchId}/phases/phase_aff_statement_2/start`);
      const page = await trackedPage(context, "console-agent");
      await setConsoleReady(page, "spk_aff_2");
      await page.goto(pageUrl("/console/spk_aff_2", { token }), { waitUntil: "domcontentloaded" });
      await expectText(page, "AI 辩手状态");
      await expectText(page, /已获得发言权限|等待当前发言结束|等待轮次/);
      await expectText(page, "提示");
      assert(await page.getByText(/当前轮次可由主持台触发|请等待主持人在主持导播台启动/).count() === 0, "AI console should not tell speaker to wait for host startup");
      assert(await page.getByRole("button", { name: /开始发言|暂停发言|继续发言|结束发言/ }).count() === 0, "AI console should not expose human speech controls");
    });

    await step("Open vote page and submit one ballot", async () => {
      await apiRequest("POST", `/api/matches/${matchId}/reset`, { confirm_text: "重置比赛" });
      await apiRequest("POST", `/api/matches/${matchId}/audience-votes/open`);
      const page = await trackedPage(context, "vote");
      await page.goto(pageUrl("/vote"), { waitUntil: "domcontentloaded" });
      await expectText(page, "人机辩论赛");
      await expectText(page, "选出你认为胜利的一方");
      await expectText(page, "分别投出立论、过程、结辩");
      await expectText(page, "给 8 位辩手排名");
      await page.locator(".vote-side-btn--affirmative").first().click();
      for (const card of await page.locator(".vote-aspect-card").all()) {
        await card.locator(".vote-aspect-btn.affirmative").click();
      }
      const rankCards = await page.locator(".vote-rank-card").all();
      for (const card of rankCards) {
        await card.click();
      }
      await page.getByRole("button", { name: "提交投票" }).click();
      await expectText(page, /已收到你的投票|投票已提交/);
      await page.reload({ waitUntil: "domcontentloaded" });
      await expectText(page, /已收到你的投票|投票已提交/);
    });

    await step("Finish match from host", async () => {
      await apiRequest("POST", `/api/matches/${matchId}/resume`).catch(() => undefined);
      await apiRequest("POST", `/api/matches/${matchId}/votes`, {
        judge_summary: {
          constructive: { affirmative: 2, negative: 1 },
          process: { affirmative: 2, negative: 1 },
          conclusion: { affirmative: 2, negative: 1 },
          winner_side: "affirmative",
          best_speaker_id: "spk_aff_3"
        }
      });
      await apiRequest("POST", `/api/matches/${matchId}/votes/publish`, { scope: "judge" });
      await apiRequest("POST", `/api/matches/${matchId}/votes/publish`, { scope: "audience" });
      const page = await trackedPage(context, "host-finish");
      await page.goto(pageUrl("/host", { token }), { waitUntil: "domcontentloaded" });
      await expectText(page, "学生结果页");
      await page.getByRole("button", { name: /宣布比赛结束/ }).click();
      await expectText(page, "确认宣布本场比赛结束？结束后主持台将锁定正常流程。");
      await page.locator(".feedback-modal").getByRole("button", { name: "确认", exact: true }).click();
      await expectText(page, "已结束");
      await expectText(page, "流程已完成");
    });
  } finally {
    await browser.close();
  }

  const failed = results.filter((item) => item.status === "fail");
  for (const item of results) {
    const mark = item.status === "ok" ? "OK" : "FAIL";
    console.log(`${mark} ${item.name}${item.detail ? ` - ${item.detail}` : ""}`);
  }
  if (failed.length) {
    process.exitCode = 1;
    return;
  }
  console.log(`Browser smoke passed against ${frontendUrl} (${matchId})`);
}

function stableChromeArgs(useFakeAudio = false, fakeAudioPath = "", homeDir = browserHomeDir()) {
  return [
    "--disable-crash-reporter",
    "--disable-crashpad",
    `--crash-dumps-dir=${path.join(homeDir, "crashes")}`,
    ...(useFakeAudio
      ? [
          "--use-fake-ui-for-media-stream",
          "--use-fake-device-for-media-stream",
          `--use-file-for-fake-audio-capture=${fakeAudioPath}`
        ]
      : [])
  ];
}

function browserHomeDir() {
  const dir = process.env.PHDEBATE_BROWSER_HOME || path.join(os.tmpdir(), "phdebate-browser-home");
  fs.mkdirSync(path.join(dir, "crashes"), { recursive: true });
  return dir;
}

async function step(name, fn) {
  try {
    await fn();
    results.push({ name, status: "ok", detail: "" });
  } catch (error) {
    results.push({ name, status: "fail", detail: error instanceof Error ? error.message : String(error) });
  }
}

async function trackedPage(context, label) {
  const page = await context.newPage();
  page.on("pageerror", (error) => {
    results.push({ name: `${label} page error`, status: "fail", detail: error.message });
  });
  page.on("console", (message) => {
    if (message.type() === "error") {
      if (isIgnorableConsoleNoise(message.text())) return;
      results.push({ name: `${label} console error`, status: "fail", detail: message.text() });
    }
  });
  return page;
}

function isIgnorableConsoleNoise(text) {
  return (
    text.includes("Failed to load resource: net::ERR_CONTENT_LENGTH_MISMATCH") ||
    text.includes("Failed to load resource: the server responded with a status of 502") ||
    text.includes("Failed to load resource: the server responded with a status of 409 (Conflict)") ||
    text.includes("Connection closed before receiving a handshake response")
  );
}

async function setConsoleReady(page, speakerIdForConsole) {
  await page.addInitScript(({ id, match }) => {
    window.localStorage.setItem(`phdebate_console_ready_${match}_${id}`, "1");
    window.localStorage.setItem(`phdebate_console_speaker_${match}`, id);
    window.localStorage.setItem(`phdebate_console_name_${match}_${id}`, id === "spk_aff_3" ? "林晚晴" : "玄思");
  }, { id: speakerIdForConsole, match: matchId });
}

async function expectText(page, textOrPattern, timeout = 20000) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    const bodyText = await page.locator("body").innerText().catch(() => "");
    const matched =
      typeof textOrPattern === "string"
        ? bodyText.includes(textOrPattern)
        : textOrPattern.test(bodyText);
    if (matched) return;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`Timed out waiting for text: ${textOrPattern}`);
}

async function apiRequest(method, path, body, options = {}) {
  const headers = {
    ...(options.auth === false || !token ? {} : { Authorization: `Bearer ${token}` }),
    ...(body ? { "Content-Type": "application/json" } : {})
  };
  const response = await fetch(`${apiUrl}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined
  });
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok || data?.ok === false) {
    const message = data?.error?.message || data?.detail || response.statusText;
    throw new Error(`${method} ${path} -> ${response.status}: ${message}`);
  }
  return data;
}

async function waitForSnapshot(predicate, timeout = 10000) {
  const started = Date.now();
  let lastState = null;
  while (Date.now() - started < timeout) {
    const body = await apiRequest("GET", `/api/matches/${matchId}`);
    lastState = body.data;
    if (predicate(lastState)) return lastState;
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error(`timed out waiting for snapshot condition${lastState ? ` at seq ${lastState.last_seq}` : ""}`);
}

function pageUrl(path, params = {}) {
  const url = new URL(path, frontendUrl);
  for (const [key, value] of Object.entries(params)) {
    if (value) url.searchParams.set(key, value);
  }
  return url.toString();
}

function findChromeExecutable() {
  const candidates = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser"
  ];
  return candidates.find((candidate) => fs.existsSync(candidate));
}

function writeFakeAudioFile() {
  const filePath = path.join(os.tmpdir(), "phdebate-browser-smoke-audio.wav");
  const sampleRate = 48000;
  const seconds = 4;
  const channels = 1;
  const bitsPerSample = 16;
  const bytesPerSample = bitsPerSample / 8;
  const sampleCount = sampleRate * seconds;
  const dataSize = sampleCount * channels * bytesPerSample;
  const header = Buffer.alloc(44);
  header.write("RIFF", 0);
  header.writeUInt32LE(36 + dataSize, 4);
  header.write("WAVE", 8);
  header.write("fmt ", 12);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(channels, 22);
  header.writeUInt32LE(sampleRate, 24);
  header.writeUInt32LE(sampleRate * channels * bytesPerSample, 28);
  header.writeUInt16LE(channels * bytesPerSample, 32);
  header.writeUInt16LE(bitsPerSample, 34);
  header.write("data", 36);
  header.writeUInt32LE(dataSize, 40);

  const data = Buffer.alloc(dataSize);
  for (let index = 0; index < sampleCount; index += 1) {
    const sample = Math.round(Math.sin((index / sampleRate) * Math.PI * 2 * 440) * 0x2000);
    data.writeInt16LE(sample, index * bytesPerSample);
  }
  fs.writeFileSync(filePath, Buffer.concat([header, data]));
  return filePath;
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

await main();
