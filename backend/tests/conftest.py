"""Test isolation: never let the developer's real provider keys reach the tests.

A NEBIUS_API_KEY in the shell switches Nemotron to Token Factory (real, billed
calls). Tests that need it set it themselves with monkeypatch.
"""
import pytest


@pytest.fixture(autouse=True)
def _no_real_provider_keys(monkeypatch):
    for name in ("NEBIUS_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
