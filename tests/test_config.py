"""Fail-fast configuration validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from sws_agent.config import (
    SWS_APPROVAL_DB_ENV,
    SWS_EXECUTION_MODE_ENV,
    AWSConnectionConfig,
    SWSRuntimeConfig,
    approval_db_from_env,
    execution_mode_from_env,
)
from sws_agent.constants import ExecutionMode, MAX_COST_WINDOW_DAYS


def test_runtime_config_defaults_to_safe_mode():
    config = SWSRuntimeConfig()
    assert config.execution_mode is ExecutionMode.SAFE


def test_runtime_config_accepts_valid_mode():
    config = SWSRuntimeConfig(execution_mode="autonomous")
    assert config.execution_mode is ExecutionMode.AUTONOMOUS


def test_runtime_config_rejects_unknown_mode():
    with pytest.raises(ValidationError):
        SWSRuntimeConfig(execution_mode="run_amok")


def test_runtime_config_rejects_zero_resources_per_request():
    with pytest.raises(ValidationError):
        SWSRuntimeConfig(resources_per_inventory_request=0)


def test_runtime_config_rejects_cost_window_above_canonical_cap():
    with pytest.raises(ValidationError):
        SWSRuntimeConfig(cost_window_days=MAX_COST_WINDOW_DAYS + 1)


def test_runtime_config_default_cost_window_within_cap():
    assert SWSRuntimeConfig().cost_window_days <= MAX_COST_WINDOW_DAYS


def test_aws_config_rejects_empty_identity():
    with pytest.raises(ValidationError):
        AWSConnectionConfig()


def test_aws_config_accepts_region_only():
    config = AWSConnectionConfig(region="us-east-1")
    assert config.region == "us-east-1"
    assert config.profile is None


def test_aws_config_accepts_profile_only():
    config = AWSConnectionConfig(profile="dev")
    assert config.profile == "dev"


def test_execution_mode_defaults_to_safe_when_env_absent(monkeypatch):
    monkeypatch.delenv(SWS_EXECUTION_MODE_ENV, raising=False)
    assert execution_mode_from_env() is ExecutionMode.SAFE


def test_execution_mode_blank_env_resolves_to_safe(monkeypatch):
    monkeypatch.setenv(SWS_EXECUTION_MODE_ENV, "   ")
    assert execution_mode_from_env() is ExecutionMode.SAFE


def test_execution_mode_parses_valid_env(monkeypatch):
    monkeypatch.setenv(SWS_EXECUTION_MODE_ENV, "autonomous")
    assert execution_mode_from_env() is ExecutionMode.AUTONOMOUS


def test_execution_mode_env_value_is_exact_lowercase(monkeypatch):
    for raw in ("safe", "review", "autonomous"):
        monkeypatch.setenv(SWS_EXECUTION_MODE_ENV, raw)
        assert execution_mode_from_env() is ExecutionMode(raw)


def test_execution_mode_env_mixed_case_fails_fast(monkeypatch):
    monkeypatch.setenv(SWS_EXECUTION_MODE_ENV, "Safe")
    with pytest.raises(ValidationError):
        execution_mode_from_env()


def test_execution_mode_unknown_env_fails_fast(monkeypatch):
    monkeypatch.setenv(SWS_EXECUTION_MODE_ENV, "run_amok")
    with pytest.raises(ValidationError):
        execution_mode_from_env()


# --- M12 Phase 3A: durable approval ledger configuration ---


def test_approval_db_absent_resolves_to_none(monkeypatch):
    """No env var means no durable approval: the caller keeps in-memory."""
    monkeypatch.delenv(SWS_APPROVAL_DB_ENV, raising=False)
    assert approval_db_from_env() is None


def test_approval_db_blank_resolves_to_none(monkeypatch):
    for raw in ("", "   ", "\t", "\n", " \t\n "):
        monkeypatch.setenv(SWS_APPROVAL_DB_ENV, raw)
        assert approval_db_from_env() is None


def test_approval_db_absolute_path_resolves_to_path(monkeypatch, tmp_path):
    target = tmp_path / "approvals.sqlite3"
    monkeypatch.setenv(SWS_APPROVAL_DB_ENV, str(target))
    resolved = approval_db_from_env()
    assert isinstance(resolved, Path)
    assert resolved == target
    assert resolved.is_absolute()


def test_approval_db_absolute_path_on_other_drive_is_accepted(monkeypatch):
    """The helper does no platform guessing; it only requires absoluteness."""
    monkeypatch.setenv(SWS_APPROVAL_DB_ENV, "D:/data/sws/approvals.sqlite3")
    resolved = approval_db_from_env()
    assert resolved is not None
    assert resolved.is_absolute()
    assert str(resolved) == str(Path("D:/data/sws/approvals.sqlite3"))


def test_approval_db_surrounding_whitespace_is_stripped(monkeypatch, tmp_path):
    """Surrounding whitespace is an artifact of shell/env quoting.

    Interior path content is untouched -- only the ends are stripped, so a
    path is never silently rewritten beyond what the operator typed.
    """
    target = tmp_path / "approvals.sqlite3"
    monkeypatch.setenv(SWS_APPROVAL_DB_ENV, f"  {target}  ")
    assert approval_db_from_env() == target


def test_approval_db_relative_path_fails_fast(monkeypatch):
    """A relative path could open a second, competing approval authority.

    Two processes launched from different working directories would resolve
    the same config value to two different ledger files. Each would look
    correct and neither would see the other's grants, so the configuration is
    rejected rather than silently normalized.
    """
    for raw in (
        "approvals.sqlite3",
        "./approvals.sqlite3",
        "state/approvals.sqlite3",
        "../approvals.sqlite3",
        "  data/approvals.sqlite3  ",
    ):
        monkeypatch.setenv(SWS_APPROVAL_DB_ENV, raw)
        with pytest.raises(ValueError, match="absolute path"):
            approval_db_from_env()


def test_approval_db_helper_creates_no_database(tmp_path, monkeypatch):
    """Resolution is pure: it must not touch the filesystem."""
    target = tmp_path / "nested" / "approvals.sqlite3"
    monkeypatch.setenv(SWS_APPROVAL_DB_ENV, str(target))

    resolved = approval_db_from_env()

    assert resolved == target
    assert not target.exists()
    assert not target.parent.exists()


def test_approval_db_helper_does_not_depend_on_durable_store():
    """The helper resolves a path; opening the ledger is the store's job."""
    from sws_agent import config as config_module

    # config.py must not construct or even reference the durable ledger, so
    # importing it stays free of any SQLite or store-construction concern.
    assert not hasattr(config_module, "DurableApprovalStore")
    assert not hasattr(config_module, "InMemoryApprovalStore")