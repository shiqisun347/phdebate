"""Realtime voice admission compatibility surface.

The Redis Lua implementation remains in ``lighttts_admission`` while callers
migrate. Keeping one implementation avoids split queue state during rollout.
"""

from app.services.lighttts_admission import (
    LightTTSAdmissionCancelled,
    LightTTSAdmissionLease,
    LightTTSAdmissionQueueFull,
    LightTTSAdmissionQueueTimeout,
    LightTTSAdmissionUnavailable,
    RedisLightTTSAdmissionGate,
    estimate_queue_wait,
    lighttts_admission_gate,
)

__all__ = [
    "LightTTSAdmissionCancelled",
    "LightTTSAdmissionLease",
    "LightTTSAdmissionQueueFull",
    "LightTTSAdmissionQueueTimeout",
    "LightTTSAdmissionUnavailable",
    "RedisLightTTSAdmissionGate",
    "estimate_queue_wait",
    "lighttts_admission_gate",
]
