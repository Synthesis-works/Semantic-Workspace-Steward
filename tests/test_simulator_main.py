"""Hermetic tests for the simulator entrypoint's environment handling.

The web port must follow ``SWS_SIM_PORT`` first, then the platform ``PORT``
injected by container hosts (Render et al.), then the 8080 local default.
Importing ``sws_agent.simulator.__main__`` binds no socket and starts nothing
(the servers run only under ``main()``). No network, no AWS, no credentials.
"""

from __future__ import annotations

from sws_agent.simulator import __main__ as sim_main


def _clear(monkeypatch) -> None:
    monkeypatch.delenv("SWS_SIM_PORT", raising=False)
    monkeypatch.delenv("PORT", raising=False)


def test_web_port_defaults_to_8080(monkeypatch) -> None:
    _clear(monkeypatch)
    assert sim_main._resolve_web_port() == 8080


def test_web_port_honors_platform_port(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("PORT", "10000")
    assert sim_main._resolve_web_port() == 10000


def test_web_port_explicit_override_beats_port(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("PORT", "10000")
    monkeypatch.setenv("SWS_SIM_PORT", "9000")
    assert sim_main._resolve_web_port() == 9000


def test_web_port_ignores_blank_values(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("SWS_SIM_PORT", "   ")
    monkeypatch.setenv("PORT", "")
    assert sim_main._resolve_web_port() == 8080
