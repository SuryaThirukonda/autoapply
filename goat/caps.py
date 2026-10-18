"""Per-company application caps some boards enforce (Waymo 5 per 60 days, Harvey 4 per 30 days, Scale AI
90 days before reapplying to the same role). Rules live in config/triage_rules.json under
"application_caps"; counts come from the application ledger (goat's own and the imported Hermes history).
A capped job is held with the date the window opens again, and the watchdog puts it back then."""
from __future__ import annotations

import time

from . import db, states
from .answers import norm
from .ledger import loose_title

PREFIX = "application_cap"


def rules() -> list[dict]:
    from .triage import rules as triage_rules
    return list(triage_rules().get("application_caps") or [])


def _matches(rule_company: str, company: str) -> bool:
    rc, c = norm(rule_company), norm(company)
    return bool(rc and c) and (c == rc or c.startswith(rc + " ") or rc in c.split(" and "))


def _apps(company: str, rule_company: str, exclude_listing: str | None) -> list:
    return [r for r in db.connect().execute("SELECT listing_id, company, title, submitted_at FROM applications WHERE submitted_at IS NOT NULL")
            if _matches(rule_company, r["company"] or "") and not (exclude_listing and r["listing_id"] == exclude_listing)]


def check(company: str, title: str, listing_id: str | None = None, now: int | None = None) -> tuple[str, int] | None:
    """(hold reason, epoch when the window reopens) when applying now would break a cap, else None."""
    now = now or db.now()
    for rule in rules():
        rc = str(rule.get("company") or "")
        if not _matches(rc, company):
            continue
        apps = _apps(company, rc, listing_id)
        days = int(rule.get("days") or 0)
        if rule.get("max") and days:
            recent = sorted(int(a["submitted_at"]) for a in apps if int(a["submitted_at"]) >= now - days * 86400)
            if len(recent) >= int(rule["max"]):
                opens = recent[len(recent) - int(rule["max"])] + days * 86400
                return (f"{PREFIX}: {rc} accepts {rule['max']} applications per {days} days and {len(recent)} went out in that window; "
                        f"it opens again {time.strftime('%Y-%m-%d', time.localtime(opens))} (requeued then)"), opens
        same = int(rule.get("same_role_days") or 0)
        if same:
            lt = loose_title(title)
            hits = sorted(int(a["submitted_at"]) for a in apps
                          if loose_title(a["title"] or "") == lt and int(a["submitted_at"]) >= now - same * 86400)
            if lt and hits:
                opens = hits[-1] + same * 86400
                return (f"{PREFIX}: {rc} asks {same} days between applications to the same role; the last went out "
                        f"{time.strftime('%Y-%m-%d', time.localtime(hits[-1]))}, so it opens {time.strftime('%Y-%m-%d', time.localtime(opens))} "
                        "(requeued then)"), opens
    return None


def hold_if_capped(job: dict) -> str | None:
    """Worker hook: hold a capped job (with its reopen time) and return the reason; None to go ahead."""
    hit = check(job.get("company") or "", job.get("title") or "", job.get("listing_id"))
    if not hit:
        return None
    reason, opens = hit
    db.meta_set(f"cap_opens:{job['id']}", opens)
    return reason


def requeue_reopened(now: int | None = None) -> int:
    """Watchdog: capped holds whose window has opened go back to the queue."""
    now = now or db.now()
    n = 0
    rows = db.connect().execute("SELECT id FROM jobs WHERE status=? AND hold_reason LIKE ?", (states.HOLD, PREFIX + ":%")).fetchall()
    for r in rows:
        opens = db.meta_get(f"cap_opens:{r['id']}", 0) or 0
        if opens and opens <= now:
            with db.tx() as con:
                con.execute("UPDATE jobs SET status=?, hold_reason=NULL, updated_at=? WHERE id=? AND status=?",
                            (states.QUEUED, now, r["id"], states.HOLD))
            db.event(r["id"], "job:requeued", {"reason": "application cap window opened"})
            n += 1
    return n
