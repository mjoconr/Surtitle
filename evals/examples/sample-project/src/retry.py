"""Retry policy for ingest, and the defaults it falls back to.

`config/service.ini` is the source of truth at run time; these are what the
loader uses when a key is missing.
"""

from __future__ import annotations

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_TIMEOUT_SECONDS = 30
BACKOFF_SECONDS = (1, 2, 4, 8)


def attempts_from(config: dict[str, str]) -> int:
    """The retry limit for a service, from its config section."""
    return int(config.get("max_attempts", DEFAULT_MAX_ATTEMPTS))
