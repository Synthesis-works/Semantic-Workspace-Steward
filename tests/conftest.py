"""Shared fixtures and hermetic test guards.

Reuses SMS's test convention: an autouse fixture snapshot and restores
``os.environ`` so no test can leak or depend on ambient environment
(e.g. AWS credentials). The suite is fully hermetic and requires no AWS
or network access.
"""

from os import environ
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _preserve_os_environment():
    """Snapshot and restore os.environ around every test."""
    with mock.patch.dict(environ, {}, clear=False):
        yield