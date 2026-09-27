"""Fail-fast configuration validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sws_agent.config import (
    SWS_EXECUTION_MODE_ENV,
    AWSConnectionConfig,
    SWSRuntimeConfig,
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