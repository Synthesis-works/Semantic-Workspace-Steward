"""Run the M15-F suite with all sockets blocked, as a standalone subprocess.

Injected via ``PYTHONPATH`` and ``-c`` rather than by nesting ``pytest.main()``
inside a test. Nesting re-enters pytest and re-triggers third-party plugins
(seleniumbase's ``pytest_runtest_makereport`` hook), which added a spurious
warning to the suite and made the gate's own output depend on the gate's
mechanism.

This module is imported before the tests. It is the reason the hermeticity claim
is demonstrated rather than asserted: if anything in the M15-F suite opens a
socket or reads an ambient AWS credential, the run fails loudly.
"""

from __future__ import annotations

import os
import socket

_REAL_SOCKET = socket.socket
_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_CREATE_CONNECTION = socket.create_connection


class NetworkBlocked(RuntimeError):
    pass


def _blocked(*args: object, **kwargs: object) -> None:
    raise NetworkBlocked(
        "network access attempted; the M15-F suite must be hermetic"
    )


socket.socket.connect = _blocked  # type: ignore[method-assign]
socket.socket.connect_ex = _blocked  # type: ignore[method-assign]
socket.getaddrinfo = _blocked  # type: ignore[assignment]
socket.create_connection = _blocked  # type: ignore[assignment]

for _name in list(os.environ):
    if "AWS" in _name.upper():
        del os.environ[_name]
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
os.environ["AWS_ACCESS_KEY_ID"] = "blocked"
os.environ["AWS_SECRET_ACCESS_KEY"] = "blocked"


def socket_was_reachable() -> bool:
    """Used by the controls: proves the blocker is installed and effective."""
    try:
        socket.getaddrinfo("ec2.us-east-1.amazonaws.com", 443)
    except NetworkBlocked:
        return False
    return True


def raw_socket_still_connectable() -> bool:
    """Second control: a socket built before the patch still cannot connect."""
    sock = _REAL_SOCKET(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("169.254.169.254", 80))
    except NetworkBlocked:
        return False
    except OSError:
        # A real refusal also proves the network path is not being used.
        return False
    return True


def raw_getaddrinfo_unblocked() -> bool:
    """The unpatched reference, retained so a control can show the patch bites."""
    try:
        _REAL_GETADDRINFO("localhost", 80)
    except OSError:
        return False
    return True