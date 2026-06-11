#!/usr/bin/env node

const baseUrl = (process.env.PHDEBATE_SMOKE_BASE_URL || process.env.PHDEBATE_BASE_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
const matchId = process.env.PHDEBATE_SMOKE_MATCH_ID || "match_001";
const token = process.env.PHDEBATE_SMOKE_TOKEN || process.env.PHDEBATE_HOST_PASSWORD || process.env.PHDEBATE_ADMIN_PASSWORD || "";

const authHeaders = token ? { Authorization: `Bearer ${token}` } : {};
const results = [];

async function main() {
  await step("API health", async () => {
    const data = await request("GET", "/api/health", undefined, { auth: false });
    assert(data.ok === true, "health ok");
  });

  await step("Initial snapshot", async () => {
    const data = await request("GET", `/api/matches/${matchId}`);
    assert(data.data.match.id === matchId, "match id");
  });

  await step("Public vote options", async () => {
    const data = await request("GET", `/api/public/matches/${matchId}/vote-options`, undefined, { auth: false });
    assert(data.data.teams.length >= 2, "vote teams");
    assert(data.data.speakers.length >= 4, "vote speakers");
  });

  await step("Screen scene switching", async () => {
    await request("POST", `/api/matches/${matchId}/screen/scene`, { scene: "teams" });
    const live = await request("POST", `/api/matches/${matchId}/screen/scene`, { scene: "live", live_mode: "free" });
    assert(live.data.match.screen_scene === "live", "live scene");
  });

  await step("Free debate human ASR flow", async () => {
    await request("POST", `/api/matches/${matchId}/resume`);
    await request("POST", `/api/matches/${matchId}/phases/phase_free_debate/start`);
    await request("POST", `/api/matches/${matchId}/speakers/spk_aff_3/activate`);
    await request("POST", `/api/matches/${matchId}/speakers/spk_aff_3/start-speaking`);
    await request("POST", `/api/matches/${matchId}/speakers/spk_aff_3/asr/partial`, {
      text: "smoke 演练 partial：自由辩论链路正在同步。",
      latency_ms: 420
    });
    const final = await request("POST", `/api/matches/${matchId}/speakers/spk_aff_3/asr/final`, {
      text: "smoke 演练 final：ASR、字幕和 transcript 写入正常。",
      latency_ms: 560
    });
    assert(final.data.current_speech.content_final.includes("smoke 演练 final"), "asr final");
    const stopped = await request("POST", `/api/matches/${matchId}/speakers/spk_aff_3/stop-speaking`);
    assert(stopped.data.current_speech === null, "speech stopped");
  });

  await step("Voting publish order", async () => {
    await request("POST", `/api/matches/${matchId}/audience-votes/open`);
    await request("POST", `/api/public/matches/${matchId}/audience-votes`, {
      token: `smoke-${Date.now()}`,
      winner_side: "affirmative",
      best_speaker_id: "spk_aff_3",
      client_fingerprint: "smoke-script"
    }, { auth: false });
    await request("POST", `/api/matches/${matchId}/votes`, {
      judge_summary: {
        constructive: { affirmative: 2, negative: 1 },
        process: { affirmative: 2, negative: 1 },
        conclusion: { affirmative: 2, negative: 1 },
        winner_side: "affirmative",
        best_speaker_id: "spk_aff_3"
      }
    });
    const judge = await request("POST", `/api/matches/${matchId}/votes/publish`, { scope: "judge" });
    assert(judge.data.vote_state.judge_published === true, "judge published");
    const audience = await request("POST", `/api/matches/${matchId}/votes/publish`, { scope: "audience" });
    assert(audience.data.vote_state.audience_published === true, "audience published");
    await request("POST", `/api/matches/${matchId}/screen/scene`, { scene: "result" });
  });

  await step("Export bundle download", async () => {
    const created = await request("POST", `/api/matches/${matchId}/exports`);
    assert(created.data.download_url, "download url");
    const response = await fetch(`${baseUrl}${created.data.download_url}`, { headers: authHeaders });
    assert(response.ok, `download ${response.status}`);
    const bytes = await response.arrayBuffer();
    assert(bytes.byteLength > 1000, "zip size");
  });

  const failed = results.filter((item) => item.status === "fail");
  for (const item of results) {
    const mark = item.status === "ok" ? "OK" : "FAIL";
    console.log(`${mark} ${item.name}${item.detail ? ` - ${item.detail}` : ""}`);
  }
  if (failed.length) {
    process.exitCode = 1;
    return;
  }
  console.log(`Smoke passed against ${baseUrl} (${matchId})`);
}

async function step(name, fn) {
  try {
    await fn();
    results.push({ name, status: "ok", detail: "" });
  } catch (error) {
    results.push({ name, status: "fail", detail: error instanceof Error ? error.message : String(error) });
  }
}

async function request(method, path, body, options = {}) {
  const headers = {
    ...(options.auth === false ? {} : authHeaders),
    ...(body ? { "Content-Type": "application/json" } : {})
  };
  const response = await fetch(`${baseUrl}${path}`, {
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

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

await main();
