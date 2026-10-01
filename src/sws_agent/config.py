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
from pathlib import Path

from .constants import (
    ExecutionMode,
    MAX_COST_WINDOW_DAYS,
    MAX_RESOURCES_PER_INVENTORY_REQUEST,
)

SWS_EXECUTION_MODE_ENV: Final[str] = "SWS_EXECUTION_MODE"
"""Environment variable naming the runtime execution mode (safe/review/autonomous)."""

SWS_AUDIT_DIR_ENV: Final[str] = "SWS_AUDIT_DIR"
"""Environment variable naming the audit ledger directory (M8 write-through)."""

SWS_APPROVAL_DB_ENV: Final[str] = "SWS_APPROVAL_DB"
"""Environment variable naming the durable approval ledger file (M12 Phase 3B)."""


def audit_dir_from_env() -> Path | None:
    """Resolve the audit ledger directory from ``SWS_AUDIT_DIR``.

    Absent or blank values opt out of durable audit persistence (the backend
    writes nothing). A non-blank value is used as-is; the store itself
    validates the path (creating parents and failing fast when unusable).
    """
    raw = os.environ.get(SWS_AUDIT_DIR_ENV)
    if raw is None or not raw.strip():
        return None
    return Path(raw.strip())


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


def approval_db_from_env() -> Path | None:
    """Resolve the durable approval ledger file from ``SWS_APPROVAL_DB``.

    Absent or blank values opt out of durable approval persistence: the caller
    keeps the in-memory store. This is deliberately opt-in for the same reason
    ``SWS_AUDIT_DIR`` is -- a hermetic, no-filesystem deployment must keep
    working, and there is no safe default location to guess at.

    A non-blank value **must** be absolute. A relative path would be resolved
    against each process's working directory, so two processes started from
    different directories would silently open two different approval
    authorities -- both would look correct and neither would see the other's
    grants. That is a correctness hazard an approval store cannot have, so the
    configuration is rejected here rather than normalized into an absolute
    path the operator did not choose.

    Fails fast with ``ValueError`` (the same style as
    ``execution_mode_from_env``): a misconfigured approval authority is an
    operator error, not something to repair silently at startup.

    Resolves the path only. It opens no database, creates no directories, and
    performs no I/O -- ``DurableApprovalStore`` owns all of that.
    """
    raw = os.environ.get(SWS_APPROVAL_DB_ENV)
    if raw is None or not raw.strip():
        return None
    path = Path(raw.strip())
    if not path.is_absolute():
        raise ValueError(
            f"{SWS_APPROVAL_DB_ENV} must be an absolute path, got "
            f"{str(path)!r}: a relative path would resolve differently per "
            "working directory and could open a second, competing approval "
            "authority"
        )
    return path