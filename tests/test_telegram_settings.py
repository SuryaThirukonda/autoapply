"""Settings tab: Telegram token under TELEGRAM_KEY, finding the chat id, sending a test. No network."""
from __future__ import annotations

import pytest

from goat import dashboard, db


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db.reset_for_tests(tmp_path / "t.db")
    p = tmp_path / ".env"
    p.write_text("OPENAI_API_KEY=sk-test\nTELEGRAM_KEY=123:abc\nTELEGRAM_OWNER_CHAT_ID=0\n", encoding="utf-8")
    monkeypatch.setattr(dashboard, "ENV_PATH", p)
    calls = []

    def fake(token, method, **params):
        calls.append((token, method, params))
        if method == "getMe":
            return {"ok": True, "result": {"username": "Autoappgoatbot"}}
        if method == "getUpdates":
            return {"ok": True, "result": fake.updates}
        if method == "sendMessage":
            return {"ok": True}
        return {"ok": False}
    fake.updates = []
    monkeypatch.setattr(dashboard, "_telegram_call", fake)
    return p, calls, fake


def test_settings_show_telegram_key_masked(env):
    s = dashboard.api_settings()
    assert "TELEGRAM_KEY" in s and s["TELEGRAM_KEY"] != "123:abc"
    assert s["_status"]["telegram"]["token"] is True and s["_status"]["telegram"]["chat_id"] is False


def test_detect_asks_for_start_when_no_one_messaged(env):
    _, _, fake = env
    r = dashboard.telegram_detect()
    assert not r["ok"] and "/start" in r["error"] and r["bot"] == "Autoappgoatbot"


def test_detect_saves_the_single_private_chat_then_test_sends(env):
    p, calls, fake = env
    fake.updates = [{"message": {"chat": {"id": 987654, "type": "private", "first_name": "Surya", "username": "surya"}}},
                    {"message": {"chat": {"id": -100, "type": "group"}}}]
    r = dashboard.telegram_detect()
    assert r["ok"] and r["chat_id"] == 987654
    assert "TELEGRAM_OWNER_CHAT_ID=987654" in p.read_text(encoding="utf-8")
    assert "TELEGRAM_KEY=123:abc" in p.read_text(encoding="utf-8")          # the token line is untouched
    t = dashboard.telegram_test()
    assert t["ok"] and calls[-1][1] == "sendMessage" and calls[-1][2]["chat_id"] == 987654


def test_detect_refuses_to_guess_between_two_people(env):
    _, _, fake = env
    fake.updates = [{"message": {"chat": {"id": 1, "type": "private"}}}, {"message": {"chat": {"id": 2, "type": "private"}}}]
    r = dashboard.telegram_detect()
    assert not r["ok"] and len(r["chats"]) == 2
