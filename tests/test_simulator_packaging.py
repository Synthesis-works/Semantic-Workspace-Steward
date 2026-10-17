"""Hermetic guard: the demo's web UI must ship as package data.

The starlette app serves ``static/{index.html,app.js,app.css}`` from a path
relative to ``app.py``. Those are non-Python files, so they are only present in
an installed wheel if ``pyproject.toml`` declares them as package data. When
that declaration is missing, running from the source tree still works (which is
why the bug went unnoticed) but a built/installed image fails at startup with
"Directory ... static does not exist". This test asserts the declaration and the
assets both exist, without building anything or touching the network.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "src" / "sws_agent" / "simulator" / "static"
ASSETS = ("index.html", "app.js", "app.css")


def _package_data_patterns() -> list[str]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        config = tomllib.load(fh)
    return config["tool"]["setuptools"]["package-data"]["sws_agent.simulator"]


def test_static_assets_exist_in_source() -> None:
    for asset in ASSETS:
        assert (STATIC_DIR / asset).is_file(), f"missing web asset: {asset}"


def test_static_assets_are_declared_as_package_data() -> None:
    patterns = _package_data_patterns()
    assert any(p.startswith("static/") for p in patterns), (
        "sws_agent.simulator must declare its static assets in "
        "[tool.setuptools.package-data] or the installed image omits the UI"
    )
