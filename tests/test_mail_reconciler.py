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
    # an earlier test's code wait can leave Gmail marked busy for 45 s; with sleep faked, the pass would never go on
    monkeypatch.setattr(gmail, "_URGENT_UNTIL", 0.0)
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


@pytest.mark.asyncio
async def test_the_pass_waits_while_a_run_reads_gmail_for_a_code(apps, monkeypatch):
    """Insperity (2026-10-23): its reset-link wait and the confirmation pass were both throttled by Gmail."""
    busy = iter([True, True, False] + [False] * 20)
    waits = []

    async def pause(s):
        waits.append(s)
    monkeypatch.setattr(gmail, "urgent_active", lambda: next(busy))
    monkeypatch.setattr(gmail, "find_confirmation_email", lambda company, after, client: None)
    monkeypatch.setattr(supervisor.asyncio, "sleep", pause)
    await supervisor.reconcile_mail_once(pause_s=0.5)
    assert waits[:2] == [5.0, 5.0]                              # it stood aside twice before the first read


def test_an_urgent_poll_marks_gmail_busy(monkeypatch):
    monkeypatch.setattr(gmail, "_URGENT_UNTIL", 0.0)
    assert not gmail.urgent_active()
    gmail.mark_urgent()
    assert gmail.urgent_active()
