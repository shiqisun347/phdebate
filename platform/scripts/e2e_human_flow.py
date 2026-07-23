#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import ssl
import time

import httpx
import websockets


async def run(base_url: str, insecure: bool) -> dict:
    suffix = f"{int(time.time())}{secrets.randbelow(10000):04d}"
    account = f"e2e_{suffix}"
    password = "E2e-test-1234"
    code: str | None = None
    async with httpx.AsyncClient(base_url=base_url, verify=not insecure, timeout=30) as client:
        headers: dict[str, str | None] = {}
        try:
            registered = await client.post(
                "/api/auth/register",
                json={"account": account, "real_name": f"回归测试{suffix[-4:]}", "password": password, "confirm_password": password},
            )
            registered.raise_for_status()
            csrf = client.cookies.get("jixia_csrf")
            headers = {"X-CSRF-Token": csrf}
            create_headers = headers | {"X-Idempotency-Key": "e2e-create"}
            create_payload = {
                "competition_slug": "training-1v1",
                "custom_topic": "真人发言端到端回归测试",
                "seat_key": "aff_1",
                "visibility": "public",
            }
            created = await client.post("/api/rooms", headers=create_headers, json=create_payload)
            created.raise_for_status()
            replayed_create = await client.post("/api/rooms", headers=create_headers, json=create_payload)
            replayed_create.raise_for_status()
            room = created.json()["room"]
            code = room["code"]
            if replayed_create.json()["room"]["code"] != code:
                raise RuntimeError("idempotent room creation returned a different room")
            initial_connected = next(seat for seat in room["seats"] if seat["is_me"])["connected"]

            cookie_header = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
            ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + f"/ws/rooms/{code}"
            ssl_context: ssl.SSLContext | None = None
            if ws_url.startswith("wss://"):
                ssl_context = ssl.create_default_context()
                if insecure:
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_NONE
            async with websockets.connect(ws_url, ssl=ssl_context, additional_headers={"Cookie": cookie_header}) as socket:
                snapshot = json.loads(await asyncio.wait_for(socket.recv(), timeout=15))
                websocket_connected = next(seat for seat in snapshot["room"]["seats"] if seat["is_me"])["connected"]
                (await client.post(f"/api/rooms/{code}/ready", headers=headers, json={"ready": True})).raise_for_status()
                (await client.post(f"/api/rooms/{code}/start", headers=headers, json={})).raise_for_status()

                opening_started_at = time.monotonic()
                deadline = opening_started_at + 45
                current = None
                while time.monotonic() < deadline:
                    current = (await client.get(f"/api/rooms/{code}")).json()["room"]
                    if current["status"] == "running" and current.get("current_stage"):
                        break
                    await asyncio.sleep(0.5)
                if not current or current["status"] != "running":
                    raise RuntimeError(f"room did not enter a running stage within 45s: {current}")
                opening_latency_seconds = round(time.monotonic() - opening_started_at, 3)
                skip_headers = headers | {"X-Idempotency-Key": "e2e-skip"}
                first_skip = await client.post(f"/api/rooms/{code}/control/skip", headers=skip_headers, json={"reason": "e2e"})
                first_skip.raise_for_status()
                repeated_skip = await client.post(f"/api/rooms/{code}/control/skip", headers=skip_headers, json={"reason": "e2e"})
                repeated_skip.raise_for_status()

                # Skipping the opening announcement advances to the next
                # stage's fixed female host cue first. A real speaker page
                # keeps its microphone disabled during that cue; mirror the
                # same contract instead of racing the server-side guard.
                speech_ready_deadline = time.monotonic() + 15
                while time.monotonic() < speech_ready_deadline:
                    after_opening = (await client.get(f"/api/rooms/{code}")).json()["room"]
                    after_stage = after_opening.get("current_stage") or {}
                    if (
                        after_opening.get("status") == "running"
                        and after_stage.get("kind") == "speech"
                        and not after_stage.get("host_announcement_pending")
                        and after_stage.get("seat") == "aff_1"
                    ):
                        break
                    await asyncio.sleep(0.25)
                else:
                    raise RuntimeError(f"human speech stage did not unlock after host cue: {after_opening}")

                start_headers = headers | {"X-Control-Lease": "e2e-device", "X-Idempotency-Key": "e2e-start"}
                lease_response = await client.post(
                    f"/api/rooms/{code}/control-lease",
                    headers=headers | {"X-Control-Lease": "e2e-device"},
                    json={},
                )
                lease_response.raise_for_status()
                first_start = await client.post(f"/api/rooms/{code}/speech/start", headers=start_headers, json={})
                first_start.raise_for_status()
                second_start = await client.post(f"/api/rooms/{code}/speech/start", headers=start_headers, json={})
                second_start.raise_for_status()
                speech_id = first_start.json()["speech_id"]

                finish_headers = headers | {"X-Control-Lease": "e2e-device", "X-Idempotency-Key": "e2e-finish"}
                transcript = "真人发言端到端测试内容。"
                first_finish = await client.post(
                    f"/api/rooms/{code}/speech/finish",
                    headers=finish_headers,
                    json={"speech_id": speech_id, "content": transcript},
                )
                first_finish.raise_for_status()
                repeated_finish = await client.post(
                    f"/api/rooms/{code}/speech/finish",
                    headers=finish_headers,
                    json={"speech_id": speech_id, "content": transcript},
                )
                repeated_finish.raise_for_status()
                conflicting_finish = await client.post(
                    f"/api/rooms/{code}/speech/finish",
                    headers=finish_headers,
                    json={"speech_id": speech_id, "content": "不应覆盖的内容"},
                )
                assert conflicting_finish.status_code == 409

                finished_room = first_finish.json()["room"]
                saved_speech = next(item for item in finished_room["speeches"] if item["id"] == speech_id)
                assert saved_speech["content"] == transcript
                assert not str(saved_speech.get("audio_url") or "").strip()

                openapi = (await client.get("/openapi.json")).json()
                retired_audio_route = f"/api/rooms/{code}/speech/{speech_id}/audio"
                assert not any(path.endswith("/speech/{speech_id}/audio") for path in openapi["paths"])
                retired_audio_response = await client.post(
                    retired_audio_route,
                    headers=headers,
                    files={"audio": ("retired.wav", b"retired", "audio/wav")},
                )
                assert retired_audio_response.status_code == 404
                terminated = await client.post(
                    f"/api/rooms/{code}/control/terminate",
                    headers=headers,
                    json={"reason": "e2e complete"},
                )
                terminated.raise_for_status()
                profile = (await client.get("/api/me")).json()

                return {
                    "account": account,
                    "room_code": code,
                    "speech_id": speech_id,
                    "initial_connected": initial_connected,
                    "websocket_connected": websocket_connected,
                    "create_replayed": replayed_create.json().get("replayed"),
                    "skip_replayed": repeated_skip.json().get("replayed"),
                    "start_replayed": second_start.json().get("replayed"),
                    "finish_replayed": repeated_finish.json().get("replayed"),
                    "conflicting_finish_status": conflicting_finish.status_code,
                    "transcript_persisted": saved_speech["content"] == transcript,
                    "human_audio_archived": bool(str(saved_speech.get("audio_url") or "").strip()),
                    "audio_upload_route_exposed": any(
                        path.endswith("/speech/{speech_id}/audio") for path in openapi["paths"]
                    ),
                    "retired_audio_route_status": retired_audio_response.status_code,
                    "terminated_in_history": any(
                        item["room_code"] == code and item["status"] == "terminated"
                        for item in profile["history"]
                    ),
                    "opening_latency_seconds": opening_latency_seconds,
                    "opening_stage": current.get("current_stage", {}).get("key"),
                }
        finally:
            # A failed assertion must not consume one of the five production room slots.
            if code:
                try:
                    room_state = (await client.get(f"/api/rooms/{code}")).json().get("room", {})
                    status = room_state.get("status")
                    if status in {"lobby", "preparing"}:
                        await client.post(f"/api/rooms/{code}/cancel", headers=headers, json={"reason": "e2e cleanup"})
                    elif status in {"running", "paused", "judging"}:
                        await client.post(f"/api/rooms/{code}/control/terminate", headers=headers, json={"reason": "e2e cleanup"})
                except Exception:
                    pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    parser.add_argument("--insecure", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.base_url.rstrip("/"), args.insecure)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
