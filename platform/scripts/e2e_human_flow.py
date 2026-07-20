#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import io
import json
import secrets
import ssl
import time
import wave

import httpx
import websockets


def wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 1600)
    return buffer.getvalue()


async def run(base_url: str, insecure: bool) -> dict:
    suffix = f"{int(time.time())}{secrets.randbelow(10000):04d}"
    account = f"e2e_{suffix}"
    password = "E2e-test-1234"
    async with httpx.AsyncClient(base_url=base_url, verify=not insecure, timeout=30) as client:
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
        created = await client.post(
            "/api/rooms",
            headers=create_headers,
            json=create_payload,
        )
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
        ssl_context = None
        if ws_url.startswith("wss://") and insecure:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        async with websockets.connect(ws_url, ssl=ssl_context, additional_headers={"Cookie": cookie_header}) as socket:
            snapshot = json.loads(await asyncio.wait_for(socket.recv(), timeout=15))
            websocket_connected = next(seat for seat in snapshot["room"]["seats"] if seat["is_me"])["connected"]
            (await client.post(f"/api/rooms/{code}/ready", headers=headers, json={"ready": True})).raise_for_status()
            (await client.post(f"/api/rooms/{code}/start", headers=headers, json={})).raise_for_status()

            deadline = time.monotonic() + 30
            current = None
            while time.monotonic() < deadline:
                current = (await client.get(f"/api/rooms/{code}")).json()["room"]
                if current["status"] == "running" and (current.get("current_stage") or {}).get("kind") == "announcement":
                    break
                await asyncio.sleep(0.5)
            if not current or current["status"] != "running":
                raise RuntimeError("room did not enter opening stage")
            skip_headers = headers | {"X-Idempotency-Key": "e2e-skip"}
            first_skip = await client.post(f"/api/rooms/{code}/control/skip", headers=skip_headers, json={"reason": "e2e"})
            first_skip.raise_for_status()
            repeated_skip = await client.post(f"/api/rooms/{code}/control/skip", headers=skip_headers, json={"reason": "e2e"})
            repeated_skip.raise_for_status()

            start_headers = headers | {"X-Control-Lease": "e2e-device", "X-Idempotency-Key": "e2e-start"}
            (
                await client.post(
                    f"/api/rooms/{code}/control-lease",
                    headers=headers | {"X-Control-Lease": "e2e-device"},
                    json={},
                )
            ).raise_for_status()
            first_start = await client.post(f"/api/rooms/{code}/speech/start", headers=start_headers, json={})
            first_start.raise_for_status()
            second_start = await client.post(f"/api/rooms/{code}/speech/start", headers=start_headers, json={})
            second_start.raise_for_status()
            speech_id = first_start.json()["speech_id"]

            finish_headers = headers | {"X-Control-Lease": "e2e-device", "X-Idempotency-Key": "e2e-finish"}
            first_finish = await client.post(
                f"/api/rooms/{code}/speech/finish",
                headers=finish_headers,
                json={"speech_id": speech_id, "content": "真人发言端到端测试内容。"},
            )
            first_finish.raise_for_status()
            second_finish = await client.post(
                f"/api/rooms/{code}/speech/finish",
                headers=finish_headers,
                json={"speech_id": speech_id, "content": "不应覆盖的内容"},
            )
            assert second_finish.status_code == 409

            invalid_audio = await client.post(
                f"/api/rooms/{code}/speech/{speech_id}/audio",
                headers=headers,
                files={"audio": ("fake.wav", b"invalid", "audio/wav")},
            )
            valid_audio = await client.post(
                f"/api/rooms/{code}/speech/{speech_id}/audio",
                headers=headers,
                files={"audio": ("recording.bin", wav_bytes(), "application/octet-stream")},
            )
            valid_audio.raise_for_status()
            repeated_audio = await client.post(
                f"/api/rooms/{code}/speech/{speech_id}/audio",
                headers=headers,
                files={"audio": ("replacement.mp3", b"ID3replacement", "audio/mpeg")},
            )
            repeated_audio.raise_for_status()
            (await client.post(f"/api/rooms/{code}/control/terminate", headers=headers, json={"reason": "e2e complete"})).raise_for_status()
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
            "finish_replayed": second_finish.json().get("replayed"),
            "invalid_audio_status": invalid_audio.status_code,
            "audio_url": valid_audio.json()["audio_url"],
            "audio_replayed": repeated_audio.json().get("replayed"),
            "terminated_in_history": any(item["room_code"] == code and item["status"] == "terminated" for item in profile["history"]),
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://117.50.192.216")
    parser.add_argument("--insecure", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.base_url.rstrip("/"), args.insecure)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
