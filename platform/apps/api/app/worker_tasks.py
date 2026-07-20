from __future__ import annotations

import logging

import dramatiq
from dramatiq.brokers.redis import RedisBroker

from app.core.config import settings
from app.services.match_archive import MatchArchiveNotFound, build_match_archive

broker = RedisBroker(url=settings.redis_url)
dramatiq.set_broker(broker)
logger = logging.getLogger(__name__)


@dramatiq.actor(max_retries=3, min_backoff=5000, time_limit=300_000)
def archive_match(match_id: str) -> None:
    """Generate a verifiable and idempotent full-match archive."""
    try:
        result = build_match_archive(match_id)
    except MatchArchiveNotFound:
        logger.info("match_archive_skipped match_id=%s reason=match_removed", match_id)
        return
    logger.info(
        "match_archived match_id=%s bytes=%s sha256=%s reused=%s",
        match_id,
        result.size_bytes,
        result.sha256,
        result.reused,
    )
