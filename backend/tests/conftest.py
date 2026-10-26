"""Pytest fixtures and test environment configuration for SubCan.

Configures an explicit test-only SESSION_SECRET and test environment mode.
Never used in production.
"""

import os
import pytest


@pytest.fixture(autouse=True)
def configure_test_environment(monkeypatch):
    """Ensure test runs have an explicit test-only session secret configured."""
    if not os.getenv("SESSION_SECRET"):
        monkeypatch.setenv("SESSION_SECRET", "test-session-secret-for-pytest-only-32bytes!")
    if not os.getenv("APP_ENV"):
        monkeypatch.setenv("APP_ENV", "test")
