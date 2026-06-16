#!/usr/bin/env node

const baseUrl = (process.env.PHDEBATE_SMOKE_BASE_URL || process.env.PHDEBATE_BASE_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
const matchId = process.env.PHDEBATE_SMOKE_MATCH_ID || "current";
const token = process.env.PHDEBATE_SMOKE_TOKEN || process.env.PHDEBATE_HOST_PASSWORD || process.env.PHDEBATE_ADMIN_PASSWORD || "";
const resetDemo = process.env.PHDEBATE_SMOKE_RESET !== "0";

const authHeaders = token ? { Authorization: `Bearer ${token}` } : {};
const results = [];

async function main() {
  await step("API health", async () => {
    const data = await request("GET", "/api/health", undefined, { auth: false });
    assert(data.ok === true, "health ok");
  });

  if (resetDemo) {
    await step("Reset demo state", async () => {
      const data = await request("POST", "/api/demo/reset");
      assert(Boolean(data.data.match.id), "demo reset match id");
      assert(data.data.match.status === "running", "demo reset running");
    });
  }

  await step("Initial snapshot", async () => {
    const data = await request("GET", `/api/matches/${matchId}`);
    if (matchId === "current") {
      assert(Boolean(data.data.match.id), "current match id");
    } else {
      assert(data.data.match.id === matchId, "match id");
    }
  });

  await step("Current match summary", async () => {
    const data = await request("GET", "/api/current-match");
    assert(Boolean(data.data.id), "current match summary id");
    assert(data.data.title.includes("人机辩论赛"), "current match summary title");
  });

  await step("Audio output setting", async () => {
    const adminOutput = await request("PUT", `/api/matches/${matchId}/audio-output`, {
      mode: "admin",
      reason: "smoke_audio_admin"
    });
    assert(adminOutput.data.audio_output.mode === "admin", "admin audio output mode");
    assert(adminOutput.data.audio_output.updated_by === "host", "audio output update actor");
    const bell = await request("POST", `/api/matches/${matchId}/bell`, {
      kind: "manual",
      label: "smoke 铃声"
    });
    assert(bell.data.audio_output.mode === "admin", "bell keeps admin output mode");
    const hostOutput = await request("PUT", `/api/matches/${matchId}/audio-output`, {
      mode: "host",
      reason: "smoke_audio_host"
    });
    assert(hostOutput.data.audio_output.mode === "host", "host audio output mode");
  });

  await step("Agent config library and binding", async () => {
    const created = await request("POST", `/api/matches/${matchId}/agents/configs`, {
      name: `smoke Agent ${Date.now()}`,
      provider_type: "rest_api",
      model_name: "Smoke-Agent-Model",
      model_kind: "closed_source",
      endpoint: "",
      timeout_ms: 10000,
      enabled: true
    });
    const config = created.data.agent_configs.find((item) => item.model_name === "Smoke-Agent-Model");
    assert(Boolean(config?.id), "created agent config id");

    const bound = await request("PATCH", `/api/matches/${matchId}/speakers/spk_aff_2`, {
      agent_config_id: config.id
    });
    const speaker = bound.data.speakers.find((item) => item.id === "spk_aff_2");
    assert(speaker.agent_config_id === config.id, "speaker agent config binding");
    assert(speaker.model_name === "Smoke-Agent-Model", "speaker model from config");

    const summary = await request("GET", `/api/matches/${matchId}/data-summary`);
    assert(summary.data.structured_counts.agent_configs >= 5, "structured agent config count");
  });

  if (matchId === "current") {
    await step("Current match reset archives old export", async () => {
      const before = await request("GET", "/api/matches/current");
      const oldMatchId = before.data.match.id;
      const oldExport = await request("POST", "/api/matches/current/exports");
      const badReset = await requestRaw("POST", "/api/matches/current/reset", { confirm_text: "确认重置" });
      assert(badReset.status === 409, `bad reset status ${badReset.status}`);
      assert(badReset.data?.error?.code === "invalid_confirmation", "bad reset error code");

      const reset = await request("POST", "/api/matches/current/reset", { confirm_text: "重置比赛" });
      assert(reset.data.match.id !== oldMatchId, "reset should create a new match id");
      assert(reset.data.match.status === "ready", "reset should prepare a ready match");
      assert(reset.data.match.screen_scene === "idle", "reset should return screen to idle");
      assert(reset.data.current_speech === null, "reset clears current speech");
      assert(reset.data.recent_transcript.length === 0, "reset clears transcripts");
      assert(reset.data.vote_state.audience_count === 0, "reset clears audience votes");
      assert(reset.data.vote_state.window_status === "closed", "reset closes vote window");

      const started = await request("POST", "/api/matches/current/start");
      assert(started.data.match.status === "running", "host start sets match running");
      assert(started.data.match.screen_scene === "live", "host start switches screen live");

      const oldResponse = await fetch(`${baseUrl}${oldExport.data.download_url}`, { headers: authHeaders });
      assert(oldResponse.ok, `old export after reset ${oldResponse.status}`);
      const oldBytes = await oldResponse.arrayBuffer();
      assert(oldBytes.byteLength > 1000, "old export zip size after reset");

      const summary = await request("GET", "/api/matches/current/data-summary");
      assert(summary.data.counts.archives >= 1, "data summary archive count");
      assert(summary.data.archives[0].export_bundle.download_url, "data summary archive export");
      assert(summary.data.request_health && summary.data.request_health.agent_status_counts, "data summary request health");
      assert(Array.isArray(summary.data.recent_events), "data summary recent events");
      assert(summary.data.event_type_counts["match.reset"] >= 1, "data summary event type counts");
    });
  }

  await step("Public vote options", async () => {
    const data = await request("GET", `/api/public/matches/${matchId}/vote-options`, undefined, { auth: false });
    assert(data.data.teams.length >= 2, "vote teams");
    assert(data.data.speakers.length >= 4, "vote speakers");
  });

  await step("Paused match locks voting", async () => {
    await request("POST", `/api/matches/${matchId}/audience-votes/open`);
    await request("POST", `/api/matches/${matchId}/pause`);
    const options = await request("GET", `/api/public/matches/${matchId}/vote-options`, undefined, { auth: false });
    assert(options.data.match.status === "paused", "paused status visible");
    const blocked = await requestRaw("POST", `/api/public/matches/${matchId}/audience-votes`, {
      token: `paused-smoke-${Date.now()}`,
      winner_side: "affirmative",
      best_speaker_id: "spk_aff_3",
      client_fingerprint: "smoke-script"
    }, { auth: false });
    assert(blocked.status === 409, `paused vote status ${blocked.status}`);
    assert(blocked.data?.error?.code === "vote_unavailable", "paused vote error");
    await request("POST", `/api/matches/${matchId}/resume`);
  });

  await step("Screen scene switching", async () => {
    const commentary = await request("POST", `/api/matches/${matchId}/screen/scene`, { scene: "judge_commentary" });
    assert(commentary.data.match.screen_scene === "judge_commentary", "commentary scene");
    assert(commentary.data.vote_state.window_status === "open", "commentary opens audience vote");
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

  await step("Timer timeout waits for host confirmation", async () => {
    await request("POST", `/api/matches/${matchId}/resume`);
    await request("POST", `/api/matches/${matchId}/phases/phase_free_debate/start`);
    await request("POST", `/api/matches/${matchId}/speakers/spk_aff_3/start-speaking`);
    await request("POST", `/api/matches/${matchId}/clocks/turn/adjust`, { remaining_ms: 0, reason: "smoke_timeout" });
    const timedOut = await waitForSnapshot((snapshot) => snapshot.flow?.awaiting_host_confirm === true);
    assert(timedOut.current_speech === null, "timeout clears current speech");
    assert(timedOut.flow.next_action === "free_turn_next", "timeout next action");
    assert(timedOut.free_debate.current_turn_side === "negative", "timeout rotates side");
    const confirmed = await request("POST", `/api/matches/${matchId}/flow/confirm`, { reason: "smoke_confirm_next_turn" });
    assert(confirmed.data.flow.awaiting_host_confirm === false, "flow confirmed");
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
    assert(audience.data.match.screen_scene === "audience_result", "audience result scene");

    const reopened = await request("POST", `/api/matches/${matchId}/audience-votes/open`);
    assert(reopened.data.window_status === "open", "reopened audience vote before finish");
    const finished = await request("POST", `/api/matches/${matchId}/finish`);
    assert(finished.data.match.status === "finished", "finish match status");
    assert(finished.data.vote_state.window_status === "closed", "finish closes audience vote");
    const blockedAfterFinish = await requestRaw("POST", `/api/public/matches/${matchId}/audience-votes`, {
      token: `finished-smoke-${Date.now()}`,
      winner_side: "affirmative",
      best_speaker_id: "spk_aff_3",
      client_fingerprint: "smoke-finished"
    }, { auth: false });
    assert(blockedAfterFinish.status === 409, `finished vote status ${blockedAfterFinish.status}`);
    assert(blockedAfterFinish.data?.error?.code === "vote_unavailable", "finished vote error");
  });

  await step("Export bundle download", async () => {
    const created = await request("POST", `/api/matches/${matchId}/exports`);
    assert(created.data.download_url, "download url");
    assert(
      created.data.entries.some((entry) => entry.path === "speech_service_requests.jsonl"),
      "speech service request export entry"
    );
    const summary = await request("GET", `/api/matches/${matchId}/data-summary`);
    const latestEntries = new Set((summary.data.latest_export?.entries || []).map((entry) => entry.path));
    assert(latestEntries.has("match.json"), "summary latest export match entry");
    assert(latestEntries.has("audio_manifest.json"), "summary latest export audio manifest entry");
    assert(latestEntries.has("structured/runtime_settings.json"), "summary latest export runtime settings entry");
    assert(!("file_path" in summary.data.latest_export), "summary latest export should hide file path");
    assert(summary.data.structured_counts.runtime_settings >= 1, "structured runtime settings count");
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
  const { response, data } = await requestRaw(method, path, body, options);
  if (!response.ok || data?.ok === false) {
    const message = data?.error?.message || data?.detail || response.statusText;
    throw new Error(`${method} ${path} -> ${response.status}: ${message}`);
  }
  return data;
}

async function requestRaw(method, path, body, options = {}) {
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
  return { response, status: response.status, data };
}

async function waitForSnapshot(predicate, timeout = 10000) {
  const started = Date.now();
  let lastSnapshot = null;
  while (Date.now() - started < timeout) {
    const data = await request("GET", `/api/matches/${matchId}`);
    lastSnapshot = data.data;
    if (predicate(lastSnapshot)) return lastSnapshot;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`timed out waiting for snapshot${lastSnapshot ? ` at seq ${lastSnapshot.last_seq}` : ""}`);
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

await main();
