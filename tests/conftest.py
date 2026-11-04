"""Ensure mock patches are cleaned after each test to prevent cross-test leakage."""

import pytest
from unittest import mock


@pytest.fixture(autouse=True)
def _cleanup_mocks():
    yield
    # After each test, stop all active patches
    try:
        mock.patch.stopall()
    except Exception:
        pass
