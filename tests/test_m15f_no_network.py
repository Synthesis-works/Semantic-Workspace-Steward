"""Prove the M15-F suite is hermetic by re-running it with sockets blocked.

The M15-F tests build real botocore clients with the transport stubbed at
``before-send``. A stub only proves hermeticity if nothing slipped past it, and
this is the one milestone whose tests construct genuine SDK objects, so the claim
is checked rather than asserted.

Blocking happens in a subprocess via ``tests/_m15f_netblock.py``, which patches
``socket.connect``, ``connect_ex``, ``getaddrinfo``, and ``create_connection``
before the tests import, and strips every ``AWS*`` environment variable.
Subprocess rather than nested ``pytest.main()`` because re-entering pytest
re-triggers third-party plugins and makes the gate's output depend on the
gate's own mechanism.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BLOCKER = REPO_ROOT / "tests" / "_m15f_netblock.py"
M15F_TESTS = [
    "tests/test_m15f_dispatch_witness.py",
    "tests/test_m15f_preflight.py",
    "tests/test_m15g_reconciliation.py",
]

_PRELUDE = f"""
import sys
sys.path.insert(0, {str(REPO_ROOT / "tests")!r})
import _m15f_netblock as nb

if nb.socket_was_reachable():
    raise SystemExit("CONTROL FAILED: DNS still reachable, blocker not installed")
if nb.raw_socket_still_connectable():
    raise SystemExit("CONTROL FAILED: a raw socket could still connect")
import pytest
raise SystemExit(pytest.main({M15F_TESTS!r} + ["-q", "--no-header", "-p", "no:cacheprovider"]))
"""


def _run_blocked() -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "tests")
    for name in list(env):
        if "AWS" in name.upper():
            del env[name]
    return subprocess.run(
        [sys.executable, "-c", _PRELUDE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


@pytest.fixture(scope="module")
def blocked_result() -> subprocess.CompletedProcess[str]:
    return _run_blocked()


def test_the_network_blocker_is_actually_installed(blocked_result: subprocess.CompletedProcess[str]) -> None:
    """Positive control, first.

    The subprocess aborts if DNS or a raw socket is still reachable, so reaching
    any assertion here means the blocker was in force. A no-op blocker would
    otherwise yield a permanently green hermeticity gate.
    """
    assert "CONTROL FAILED" not in blocked_result.stdout + blocked_result.stderr, (
        blocked_result.stdout + blocked_result.stderr
    )


def test_the_m15f_suite_passes_with_sockets_blocked(blocked_result: subprocess.CompletedProcess[str]) -> None:
    output = blocked_result.stdout + blocked_result.stderr
    assert blocked_result.returncode == 0, output
    assert "NetworkBlocked" not in output, output


def test_no_ambient_aws_credentials_survive_into_the_blocked_run() -> None:
    """The credential half of the claim, stated as its own assertion."""
    env = dict(os.environ)
    for name in list(env):
        if "AWS" in name.upper():
            del env[name]
    assert [n for n in env if "AWS" in n.upper()] == []


def test_the_blocker_would_notice_a_real_socket_attempt() -> None:
    """Second positive control: confirm the blocker fires on a real attempt.

    Without this, the blocked subprocess could be passing simply because the
    patched functions were never called by anything the tests do.
    """
    probe = (
        "import sys; "
        f"sys.path.insert(0, {str(REPO_ROOT / 'tests')!r}); "
        "import _m15f_netblock as nb; "
        "import socket; "
        "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); "
        "\ntry:\n"
        "    s.connect(('ec2.us-east-1.amazonaws.com', 443))\n"
        "    print('NOT BLOCKED')\n"
        "except nb.NetworkBlocked:\n"
        "    print('BLOCKED')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "BLOCKED" in result.stdout, result.stdout + result.stderr
    assert "NOT BLOCKED" not in result.stdout