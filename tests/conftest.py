"""Shared test settings. The agent's .env may turn the verified auto-submit on (AUTO_SUBMIT=verified) for real runs;
every test starts with it off, and the auto-submit tests switch it on themselves."""
import pytest

from goat.config import SETTINGS


@pytest.fixture(autouse=True)
def _auto_submit_off(monkeypatch):
    monkeypatch.setattr(SETTINGS, "auto_submit", "off")
