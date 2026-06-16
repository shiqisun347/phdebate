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
const token =
  process.env.PHDEBATE_BROWSER_SMOKE_TOKEN ||
  process.env.PHDEBATE_SMOKE_TOKEN ||
  process.env.PHDEBATE_ADMIN_PASSWORD ||
  process.env.PHDEBATE_HOST_PASSWORD ||
  "";
const outputDir = process.env.PHDEBATE_UI_AUDIT_DIR || path.join("artifacts", "ui-audit");

const viewports = [
  { id: "1366x768", width: 1366, height: 768, routes: ["nav", "host", "host_timeout", "admin", "admin_matches", "admin_setup", "admin_speakers", "admin_agents", "admin_agent_create", "admin_speech", "admin_data", "admin_data_detail", "screen", "screen_paused", "screen_commentary", "screen_judge_result", "screen_audience_result", "console", "console_human_ready", "console_agent_ready", "vote", "vote_paused"] },
  { id: "1440x900", width: 1440, height: 900, routes: ["host", "admin", "admin_matches", "admin_setup", "admin_speakers", "admin_agents", "admin_data", "screen", "screen_commentary", "screen_judge_result", "screen_audience_result"] },
  { id: "1920x1080", width: 1920, height: 1080, routes: ["screen", "screen_paused", "screen_commentary", "screen_judge_result", "screen_audience_result"] },
  { id: "390x844", width: 390, height: 844, routes: ["nav", "admin", "admin_matches", "admin_setup", "admin_speakers", "admin_agents", "admin_agent_create", "admin_data", "admin_data_detail", "console", "console_human_ready", "console_agent_ready", "vote", "vote_paused"] }
];

const routes = {
  nav: { path: "/", readyText: "现场导航" },
  host: { path: "/host", readyText: "当前环节", connectedText: "WS 已连接" },
  host_timeout: { path: "/host", readyText: "等待主持确认下一轮", connectedText: "WS 已连接" },
  admin: { path: "/admin", readyText: "总览监控", connectedText: "实时 已连接" },
  admin_matches: { path: "/admin", readyText: "比赛实例", connectedText: "实时 已连接", navButton: "比赛管理" },
  admin_setup: { path: "/admin", readyText: "展示信息", connectedText: "实时 已连接", navButton: "展示与赛制" },
  admin_speakers: { path: "/admin", readyText: "固定席位管理", connectedText: "实时 已连接", navButton: "辩手管理" },
  admin_agents: { path: "/admin", readyText: "Agent 管理", connectedText: "实时 已连接", navButton: "Agent 管理" },
  admin_agent_create: { path: "/admin", readyText: "创建 Agent", connectedText: "实时 已连接", navButton: "Agent 管理", modalButton: "新增 Agent" },
  admin_speech: { path: "/admin", readyText: "语音链路", connectedText: "实时 已连接", navButton: "TTS/ASR" },
  admin_data: { path: "/admin", readyText: "历史归档", connectedText: "实时 已连接", navButton: "数据管理" },
  admin_data_detail: { path: "/admin", readyText: "定位详情", connectedText: "实时 已连接", navButton: "数据管理", replayDetail: true },
  screen: { path: "/screen", readyText: "实时转写" },
  screen_paused: { path: "/screen", readyText: "比赛暂停" },
  screen_commentary: { path: "/screen", readyText: "评委点评" },
  screen_judge_result: { path: "/screen", readyText: "官方评委结果" },
  screen_audience_result: { path: "/screen", readyText: "学生投票结果" },
  console: { path: "/console", readyText: "身份选择" },
  console_human_ready: { path: "/console/spk_aff_3", readyText: "当前阶段", speakerId: "spk_aff_3" },
  console_agent_ready: { path: "/console/spk_aff_2", readyText: "AI 辩手状态", speakerId: "spk_aff_2" },
  vote: { path: "/vote", readyText: "提交投票" },
  vote_paused: { path: "/vote", readyText: "比赛暂停" }
};

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

async function main() {
  fs.mkdirSync(outputDir, { recursive: true });
  const executablePath = process.env.PHDEBATE_BROWSER_EXECUTABLE || findChromeExecutable();
  const homeDir = browserHomeDir();
  const browser = await chromium.launch({
    headless: process.env.PHDEBATE_BROWSER_HEADLESS !== "0",
    args: stableChromeArgs(homeDir),
    env: { ...process.env, HOME: homeDir },
    ...(executablePath ? { executablePath } : {})
  }).catch((error) => {
    throw new Error(
      `无法启动浏览器：${error.message}\n` +
      "可设置 PHDEBATE_BROWSER_EXECUTABLE 指向 Chrome/Chromium，或安装 Playwright 浏览器后重试。"
    );
  });
  await apiRequest("POST", "/api/demo/reset").catch(() => undefined);
  await apiRequest("POST", "/api/matches/current/audience-votes/open").catch(() => undefined);

  const report = {
    generated_at: new Date().toISOString(),
    frontend_url: frontendUrl,
    checks: [],
    summary: { total: 0, failed: 0 }
  };

  try {
    for (const viewport of viewports) {
      const context = await browser.newContext({
        viewport: { width: viewport.width, height: viewport.height },
        ignoreHTTPSErrors: true
      });
      for (const routeId of viewport.routes) {
        const route = routes[routeId];
        const page = await context.newPage();
        await prepareRoute(routeId);
        if (route.speakerId) {
          await setConsoleReady(page, route.speakerId);
        }
        const check = await auditPage(page, routeId, route, viewport);
        report.checks.push(check);
        report.summary.total += 1;
        if (check.status !== "ok") report.summary.failed += 1;
        await page.close();
      }
      await context.close();
    }
  } finally {
    await browser.close();
  }

  const reportPath = path.join(outputDir, "report.json");
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2), "utf-8");
  for (const check of report.checks) {
    const mark = check.status === "ok" ? "OK" : "FAIL";
    console.log(`${mark} ${check.viewport} ${check.route} ${check.screenshot}`);
    for (const issue of check.issues) console.log(`  - ${issue}`);
  }
  console.log(`UI audit report written to ${reportPath}`);
  if (report.summary.failed) process.exitCode = 1;
}

function stableChromeArgs(homeDir) {
  return [
    "--disable-crash-reporter",
    "--disable-crashpad",
    `--crash-dumps-dir=${path.join(homeDir, "crashes")}`
  ];
}

function browserHomeDir() {
  const dir = process.env.PHDEBATE_BROWSER_HOME || path.join(os.tmpdir(), "phdebate-browser-home");
  fs.mkdirSync(path.join(dir, "crashes"), { recursive: true });
  return dir;
}

async function auditPage(page, routeId, route, viewport) {
  const issues = [];
  const url = pageUrl(route.path);
  await page.goto(url, { waitUntil: "domcontentloaded" });
  if (route.navButton) {
    await page.getByRole("button", { name: route.navButton }).click();
  }
  if (route.modalButton) {
    await page.getByRole("button", { name: route.modalButton }).click();
  }
  if (route.replayDetail) {
    const row = page.locator(".event-row, .audit-row, .replay-row").first();
    await row.waitFor({ state: "visible", timeout: 10000 });
    await row.click();
  }
  await expectText(page, route.readyText);
  if (route.connectedText) {
    await expectText(page, route.connectedText, 8000).catch(() => {
      issues.push(`实时连接状态未就绪：未出现 "${route.connectedText}"`);
    });
  }
  await page.waitForTimeout(500);

  const metrics = await page.evaluate(() => {
    const doc = document.documentElement;
    const body = document.body;
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    const offenders = Array.from(document.querySelectorAll("body *"))
      .map((element) => {
        const rect = element.getBoundingClientRect();
        const style = window.getComputedStyle(element);
        const label = [
          element.tagName.toLowerCase(),
          element.id ? `#${element.id}` : "",
          element.className && typeof element.className === "string"
            ? `.${element.className.trim().split(/\s+/).slice(0, 3).join(".")}`
            : ""
        ].join("");
        return {
          label,
          text: (element.textContent || "").trim().replace(/\s+/g, " ").slice(0, 80),
          left: Math.round(rect.left),
          right: Math.round(rect.right),
          top: Math.round(rect.top),
          bottom: Math.round(rect.bottom),
          width: Math.round(rect.width),
          height: Math.round(rect.height),
          position: style.position,
          overflowX: style.overflowX
        };
      })
      .filter((item) => item.width > 1 && item.height > 1)
      .filter((item) => item.left < -2 || item.right > viewportWidth + 2)
      .slice(0, 12);
    return {
      viewportWidth,
      viewportHeight,
      scrollWidth: Math.max(doc.scrollWidth, body.scrollWidth),
      scrollHeight: Math.max(doc.scrollHeight, body.scrollHeight),
      offenders
    };
  });

  if (metrics.scrollWidth > viewport.width + 2) {
    issues.push(`页面横向溢出：scrollWidth=${metrics.scrollWidth}, viewport=${viewport.width}`);
  }
  for (const offender of metrics.offenders) {
    issues.push(`元素越界：${offender.label} left=${offender.left} right=${offender.right} text="${offender.text}"`);
  }
  for (const issue of await visibleTextIssues(page, routeId)) {
    issues.push(issue);
  }

  const screenshot = `${viewport.id}-${routeId}.png`;
  await page.screenshot({ path: path.join(outputDir, screenshot), fullPage: true });
  return {
    route: routeId,
    path: route.path,
    viewport: viewport.id,
    status: issues.length ? "fail" : "ok",
    issues,
    metrics,
    screenshot
  };
}

async function visibleTextIssues(page, routeId) {
  const routeGroup = routeId.split("_")[0];
  if (!["screen", "console", "vote", "nav", "host", "admin"].includes(routeGroup)) return [];
  const bodyText = await page.locator("body").innerText().catch(() => "");
  const normalized = bodyText.replace(/\s+/g, " ");
  const forbidden = ["host", "admin"].includes(routeGroup) ? [] : [
    { pattern: /\bexpired\b/i, label: "expired" },
    { pattern: /\bconnecting\b/i, label: "connecting" },
    { pattern: /\breconnecting\b/i, label: "reconnecting" },
    { pattern: /\bdenied\b/i, label: "denied" },
    { pattern: /\bidle\b/i, label: "idle" },
    { pattern: /\brunning\b/i, label: "running" },
    { pattern: /\bpaused\b/i, label: "paused" },
    { pattern: /\bmatch_[0-9A-Za-z_:-]+\b/i, label: "match_id" },
    { pattern: /\bASR\s+ok\b/i, label: "ASR ok" },
    { pattern: /\bmic(?:rophone)?\s+denied\b/i, label: "mic denied" }
  ];
  if (routeGroup === "screen") {
    forbidden.push({ pattern: /\bWS\b/i, label: "WS" });
    forbidden.push({ pattern: /\bopen\b/i, label: "open" });
  }
  const issues = forbidden
    .filter((item) => item.pattern.test(normalized))
    .map((item) => `现场页面露出英文技术状态词：${item.label}`);
  if (routeGroup === "host" || routeGroup === "admin") {
    const statusText = (await page.locator(
      ".ops-header-status, .ops-kpi-row, .rehearsal-panel, .host-status-rail, .host-now"
    ).allInnerTexts().catch(() => [])).join(" ").replace(/\s+/g, " ");
    const statusForbidden = [
      { pattern: /\bseq\b/i, label: "seq" },
      { pattern: /\bWS\s+(?:open|closed|connecting|reconnecting)\b/i, label: "WS status" },
      { pattern: /\b(?:running|paused|idle|connecting|reconnecting)\b/i, label: "raw status enum" },
      { pattern: /\bASR\s+ok\b/i, label: "ASR ok" },
      { pattern: /\bTTS\s+idle\b/i, label: "TTS idle" }
    ];
    for (const item of statusForbidden) {
      if (item.pattern.test(statusText)) issues.push(`控制台关键状态露出英文枚举：${item.label}`);
    }
  }
  return issues;
}

async function expectText(page, text, timeout = 20000) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    const bodyText = await page.locator("body").innerText().catch(() => "");
    if (bodyText.includes(text)) return;
    await page.waitForTimeout(200);
  }
  throw new Error(`Timed out waiting for text: ${text}`);
}

async function prepareRoute(routeId) {
  if (routeId === "host_timeout") {
    await apiRequest("POST", "/api/matches/current/resume").catch(() => undefined);
    await apiRequest("POST", "/api/matches/current/phases/phase_free_debate/start");
    await apiRequest("POST", "/api/matches/current/speakers/spk_aff_3/start-speaking");
    await apiRequest("POST", "/api/matches/current/clocks/turn/adjust", { remaining_ms: 0, reason: "ui_audit_timeout" });
    await waitForApiSnapshot((snapshot) => snapshot.flow?.awaiting_host_confirm === true);
    return;
  }
  if (routeId === "admin_data" || routeId === "admin_data_detail") {
    await apiRequest("POST", "/api/matches/current/exports").catch(() => undefined);
    return;
  }
  if (routeId === "vote") {
    await apiRequest("POST", "/api/matches/current/resume").catch(() => undefined);
    await apiRequest("POST", "/api/matches/current/audience-votes/open").catch(() => undefined);
    return;
  }
  if (routeId === "vote_paused") {
    await apiRequest("POST", "/api/matches/current/resume").catch(() => undefined);
    await apiRequest("POST", "/api/matches/current/audience-votes/open").catch(() => undefined);
    await apiRequest("POST", "/api/matches/current/pause");
    return;
  }
  if (!routeId.startsWith("screen")) return;
  if (routeId !== "screen_paused") {
    await apiRequest("POST", "/api/matches/current/resume").catch(() => undefined);
  }
  if (routeId === "screen") {
    await apiRequest("POST", "/api/matches/current/screen/scene", { scene: "live", live_mode: "free" });
  } else if (routeId === "screen_paused") {
    await apiRequest("POST", "/api/matches/current/pause");
  } else if (routeId === "screen_commentary") {
    await apiRequest("POST", "/api/matches/current/screen/scene", { scene: "judge_commentary" });
  } else if (routeId === "screen_judge_result") {
    await apiRequest("POST", "/api/matches/current/votes/publish", { scope: "judge" });
  } else if (routeId === "screen_audience_result") {
    await apiRequest("POST", "/api/matches/current/votes/publish", { scope: "judge" });
    await apiRequest("POST", "/api/matches/current/votes/publish", { scope: "audience" });
  }
}

async function setConsoleReady(page, speakerId) {
  await page.addInitScript(({ id }) => {
    const match = "current";
    window.localStorage.setItem(`phdebate_console_ready_${match}_${id}`, "1");
    window.localStorage.setItem(`phdebate_console_speaker_${match}`, id);
    window.localStorage.setItem(`phdebate_console_name_${match}_${id}`, id === "spk_aff_3" ? "林晚晴" : "玄思");
  }, { id: speakerId });
}

async function apiRequest(method, requestPath, body) {
  const headers = {
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(body ? { "Content-Type": "application/json" } : {})
  };
  const response = await fetch(`${apiUrl}${requestPath}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined
  });
  if (!response.ok) throw new Error(`${method} ${requestPath} -> ${response.status}`);
  return response.json();
}

async function waitForApiSnapshot(predicate, timeout = 10000) {
  const started = Date.now();
  let lastSeq = "";
  while (Date.now() - started < timeout) {
    const data = await apiRequest("GET", "/api/matches/current");
    lastSeq = String(data.data?.last_seq ?? "");
    if (predicate(data.data)) return data.data;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`Timed out waiting for API snapshot ${lastSeq}`);
}

function pageUrl(routePath) {
  const url = new URL(routePath, frontendUrl);
  if (token) url.searchParams.set("token", token);
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
