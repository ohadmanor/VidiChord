"""Source separation: a mix split into vocals, drums, bass and other.

Optional, like madmom - see :mod:`vidichord.stems.demucs_engine` for why, and
for what the app does without it.
"""

from .demucs_engine import (
    DEFAULT_MODEL,
    Separation,
    demucs_installed,
    device_name,
    model_name,
    separate,
    unavailable_reason,
)

__all__ = [
    "DEFAULT_MODEL",
    "Separation",
    "demucs_installed",
    "device_name",
    "model_name",
    "separate",
    "unavailable_reason",
]
