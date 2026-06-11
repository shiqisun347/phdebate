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
const matchId = process.env.PHDEBATE_BROWSER_MATCH_ID || process.env.PHDEBATE_SMOKE_MATCH_ID || "match_001";
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
  const browser = await chromium.launch({
    headless,
    args: audioSmoke
      ? [
          "--use-fake-ui-for-media-stream",
          "--use-fake-device-for-media-stream",
          `--use-file-for-fake-audio-capture=${fakeAudioPath}`
        ]
      : [],
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
        assert(data.data.match.id === matchId, "demo reset match id");
      });
    }

    await step("Open admin and run speech diagnostics", async () => {
      const page = await trackedPage(context, "admin");
      await page.goto(pageUrl("/admin", { match_id: matchId, token }), { waitUntil: "networkidle" });
      await expectText(page, "比赛监控");
      await expectText(page, "赛前体检报告");
      await page.getByRole("button", { name: /^刷新$/ }).click();
      await expectText(page, /最近检查：|项提醒|阻断问题|体检通过/);
      await page.getByRole("button", { name: /比赛设置/ }).click();
      await expectText(page, "现场入口分发");
      await page.getByRole("button", { name: /比赛监控/ }).click();
      await page.getByRole("button", { name: /配置检查/ }).click();
      await expectText(page, "讯飞配置诊断");
      await expectText(page, /真实服务就绪|mock 降级可用|需要处理/);
    });

    await step("Open screen live page", async () => {
      const page = await trackedPage(context, "screen");
      await page.goto(pageUrl("/screen", { match_id: matchId, token }), { waitUntil: "networkidle" });
      await expectText(page, "自由辩论");
      await expectText(page, "当前发言");
      await expectText(page, "实时转写");
    });

    await step(audioSmoke ? "Open speaker console and record PCM archive" : "Open speaker console", async () => {
      const page = await trackedPage(context, "console");
      await page.goto(pageUrl(`/console/${speakerId}`, { match_id: matchId, token }), { waitUntil: "networkidle" });
      await expectText(page, "当前环节");
      await expectText(page, "ASR");
      await expectText(page, /开始发言|结束发言|轮到你或本方发言|发言中/);
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

    await step("Open vote page and submit one ballot", async () => {
      await apiRequest("POST", `/api/matches/${matchId}/audience-votes/open`);
      const page = await trackedPage(context, "vote");
      await page.goto(pageUrl(`/vote/${matchId}`), { waitUntil: "networkidle" });
      await expectText(page, "优胜方");
      await expectText(page, "最佳辩手");
      await page.getByRole("button", { name: "提交投票" }).click();
      await expectText(page, "已收到投票");
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
      results.push({ name: `${label} console error`, status: "fail", detail: message.text() });
    }
  });
  return page;
}

async function expectText(page, textOrPattern, timeout = 10000) {
  await page.getByText(textOrPattern, { exact: false }).first().waitFor({ state: "visible", timeout });
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
