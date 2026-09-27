"""Fail-fast SWS configuration.

REUSE DECISION (see docs/reuse-decisions.md):

- Reused (pattern): SMS's validate_memory_config() fail-fast idea -- a
  partially configured or out-of-bounds subsystem raises at construction
  time instead of silently misbehaving.
- Rewritten: the configuration schema is fresh for SWS.

Configuration bounds reference the canonical limits in constants.py so
there is a single source of truth.
"""

from __future__ import annotations

import os
from typing import Final

from pydantic import BaseModel, Field, model_validator

from .constants import (
    ExecutionMode,
    MAX_COST_WINDOW_DAYS,
    MAX_RESOURCES_PER_INVENTORY_REQUEST,
)

SWS_EXECUTION_MODE_ENV: Final[str] = "SWS_EXECUTION_MODE"
"""Environment variable naming the runtime execution mode (safe/review/autonomous)."""


class SWSRuntimeConfig(BaseModel):
    """Runtime bounds and operating mode for SWS.

    Fails fast: values outside canonical limits raise a ValidationError.
    """

    execution_mode: ExecutionMode = ExecutionMode.SAFE
    resources_per_inventory_request: int = Field(
        default=MAX_RESOURCES_PER_INVENTORY_REQUEST, ge=1
    )
    cost_window_days: int = Field(
        default=MAX_COST_WINDOW_DAYS, ge=1, le=MAX_COST_WINDOW_DAYS
    )


class AWSConnectionConfig(BaseModel):
    """Connection identity for future AWS integrations.

    At least a region or a profile must be configured; an empty config is
    invalid (fail-fast). No credentials are ever stored in configuration.

    The unit-test suite never requires live AWS credentials and never
    constructs this with real secrets.
    """

    region: str | None = Field(default=None, min_length=1)
    profile: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _require_identity(self) -> "AWSConnectionConfig":
        if not self.region and not self.profile:
            raise ValueError(
                "no AWS region or profile configured; provide at least one"
            )
        return self


def execution_mode_from_env() -> ExecutionMode:
    """Resolve the runtime execution mode from ``SWS_EXECUTION_MODE``.

    Absent or blank values resolve to the canonical ``SAFE`` default.
    Anything else is validated through ``SWSRuntimeConfig`` (M7 consumes its
    ``execution_mode`` field for the first time), so out-of-vocabulary modes
    fail fast instead of being silently accepted.
    """
    raw = os.environ.get(SWS_EXECUTION_MODE_ENV)
    if raw is None or not raw.strip():
        return ExecutionMode.SAFE
    return SWSRuntimeConfig(execution_mode=raw).execution_mode