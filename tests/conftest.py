"""Shared test environment: deterministic fake server secrets for every test."""

import pytest


@pytest.fixture(autouse=True)
def studio_test_environment(monkeypatch):
    """Never let the test suite read real secrets from .env."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("STUDIO_ACCESS_KEY", "test-owner-passphrase")
    monkeypatch.delenv("DEEPSEEK_INPUT_PER_M", raising=False)
    monkeypatch.delenv("DEEPSEEK_OUTPUT_PER_M", raising=False)
