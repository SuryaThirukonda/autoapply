"""The confirmation-mail pass after every restart read Gmail unpaced and hit 'rateLimitExceeded' (2026-10-23), and one
failed read ended the whole pass. Each application is now read on its own, paced, and a failure skips only that one."""
import time

import pytest

from goat import db, ledger, supervisor
from goat.config import SETTINGS
from goat.mail import gmail


@pytest.fixture()
def apps(tmp_path, monkeypatch):
    db.reset_for_tests(tmp_path / "t.db")
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(SETTINGS.mail, "token_json", str(token))
    old = int(time.time()) - 7200
    ids = [ledger.record_application(None, co, "Intern", f"https://jobs.example.com/{co}", "agent", submitted_at=old)
           for co in ("Alpha", "Bravo", "Charlie")]
    yield ids
    db.reset_for_tests(tmp_path / "t2.db")


@pytest.mark.asyncio
async def test_one_failed_read_skips_only_that_application(apps, monkeypatch):
    seen, pauses = [], []

    def find(company, after, client):
        seen.append(company)
        if company == "Alpha":
            raise gmail.MailUnavailable("Gmail get failed (HttpError, rate limited)")
        return "mail-" + company

    async def pause(s):
        pauses.append(s)
    monkeypatch.setattr(gmail, "find_confirmation_email", find)
    monkeypatch.setattr(supervisor.asyncio, "sleep", pause)
    assert await supervisor.reconcile_mail_once(pause_s=0.5) == 2
    assert seen == ["Alpha", "Bravo", "Charlie"] and pauses == [0.5, 0.5]
    left = [r["company"] for r in ledger.unconfirmed_older_than(60)]
    assert left == ["Alpha"]                                   # tried again on the next pass
