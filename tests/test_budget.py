"""Runs are never cut short on turns: BUDGET_STEPS=0 means no step cap; the wall clock still applies."""
from goat import db
from goat.budget import Budget, BudgetExceeded
from goat.config import SETTINGS


def test_no_step_cap_by_default(tmp_path, monkeypatch):
    db.reset_for_tests(tmp_path / "t.db")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('L1','Acme','SWE','https://x/1','workday')")
        jid = con.execute("INSERT INTO jobs(listing_id,status,lane,created_at,updated_at) VALUES('L1','filling','account',1,1)").lastrowid
    monkeypatch.setattr(SETTINGS.budgets, "max_steps", 0)
    b = Budget(jid, "workday")
    for _ in range(500):
        b.step()
    assert b.steps == 500
    monkeypatch.setattr(SETTINGS.budgets, "max_steps", 3)
    b2 = Budget(jid, "workday")
    b2.step(); b2.step(); b2.step()
    try:
        b2.step()
        raise AssertionError("a positive cap must still apply")
    except BudgetExceeded:
        pass
