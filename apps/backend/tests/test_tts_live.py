import asyncio

from app.services.tts_live import TTSLiveManager


def test_tts_live_subscriber_replays_ready_chunks_and_done() -> None:
    async def run() -> None:
        manager = TTSLiveManager(ttl_seconds=1)
        key = ("match_001", "speech_001", "task_001", 0)

        await manager.start(key, {"mime_type": "audio/mpeg", "sentence_idx": 0})
        await manager.publish_chunk(key, b"abc", 1)
        await manager.finish(key, {"chunk_count": 1})

        items = []
        async for item in manager.subscribe(key):
            items.append(item)

        assert [item["type"] for item in items] == ["ready", "chunk", "done"]
        assert items[0]["mime_type"] == "audio/mpeg"
        assert items[1]["audio_base64"] == "YWJj"
        assert items[2]["chunk_count"] == 1

    asyncio.run(run())
