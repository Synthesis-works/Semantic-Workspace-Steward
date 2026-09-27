"""Timezone-aware clock and id sources used by hermetic SWS modules.

SWS core modules keep their imports relative (a test enforces that the
planner, e.g. workflow.py, never performs an absolute import so it can never
accidentally reach for network/boto/LLM machinery). This private module owns
the stdlib-only absolute imports for a UTC-aware "now" and hex-id generation
so those modules can consume them through a single relative import.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4


def utc_now() -> datetime:
    """Current UTC time as an aware datetime (injectable clock default)."""
    return datetime.now(timezone.utc)


def new_id() -> str:
    """A fresh 128-bit hex identifier (injectable id-source default)."""
    return uuid4().hex