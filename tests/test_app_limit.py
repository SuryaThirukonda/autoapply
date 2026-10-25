"""Coinbase (2026-10-25): its posting allows 3 applications per 6 months; the run held it as 'eligibility ... application
ledger not checked' while the ledger held two. A hold quoting a board's limit is counted against the ledger."""
import pytest

from goat import db, ledger
from goat.agent.tools import application_limit_room
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

REASON = "eligibility: Coinbase application limit is maximum 3 applications within 6 months; application ledger not checked"


def _sent(company, title, days_ago):
    ledger.record_application(None, company, title, f"https://example.com/{company}/{title.replace(' ', '-')}", "goat",
                              submitted_at=db.now() - days_ago * 86400)


def test_limit_room_counts_this_company_inside_the_window(store):
    _sent("Coinbase", "Software Engineer Intern", 15)
    _sent("Coinbase, Inc.", "Forward Deployed Engineer Intern", 6)
    _sent("Coinbase", "Old Intern", 400)
    _sent("Kraken", "Intern", 3)
    assert application_limit_room(REASON, "Coinbase") == (2, 3)
    assert application_limit_room("eligibility: up to 5 applications in a 60 day period", "Coinbase") == (2, 5)
    assert application_limit_room("eligibility: requires a master's degree", "Coinbase") is None


@pytest.mark.asyncio
async def test_a_limit_hold_with_room_is_bounced(store):
    _sent("Coinbase", "Software Engineer Intern", 15)
    _sent("Coinbase", "Forward Deployed Engineer Intern", 6)
    d, ctx = _dispatcher(_job("Coinbase", "C1"))

    async def page_text(n=0):
        return "Data Science Intern Apply"
    ctx.ex.page_text = page_text
    out, _ = await d.t_park(REASON)
    assert "ledger shows 2" in out["error"] and "limit is 3" in out["error"]


@pytest.mark.asyncio
async def test_a_limit_hold_at_the_limit_stands(store):
    for i in range(3):
        _sent("Coinbase", f"Intern {i}", 10 + i)
    d, ctx = _dispatcher(_job("Coinbase", "C2"))

    async def page_text(n=0):
        return "Data Science Intern Apply"
    ctx.ex.page_text = page_text
    with pytest.raises(Exception) as e:
        await d.t_park(REASON)
    assert "eligibility" in str(getattr(e.value, "reason", e.value))


@pytest.mark.asyncio
async def test_an_unsure_eligibility_hold_is_bounced_once(store):
    """McKesson (2026-10-25): 'eligibility: role is Colorado-based 10-week summer internship, but seasonal dates not
    independently requirement?' named no requirement he fails."""
    from goat.budget import Parked
    d, ctx = _dispatcher(_job("McKesson", "M1"))

    async def page_text(n=0):
        return "Software Installation & IT Support Intern Apply"
    ctx.ex.page_text = page_text
    reason = "eligibility: role is Colorado-based 10-week summer internship, but seasonal dates not independently requirement? "
    out, _ = await d.t_park(reason)
    assert "unsure" in out["error"]
    with pytest.raises(Parked):
        await d.t_park(reason)
