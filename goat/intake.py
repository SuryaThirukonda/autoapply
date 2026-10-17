"""Listings Surya adds by hand (from the website, through the cloud link). One function: check the
posting against the queue and the application ledger, then add a queued job ahead of the scraped ones."""
from __future__ import annotations

import hashlib
from urllib.parse import urlparse

from . import db, ledger, states, triage

MANUAL_PRIORITY = 50     # ahead of scraped jobs (0) and released holds (+10)


def _clean_url(url: str) -> str | None:
    u = (url or "").strip()
    try:
        p = urlparse(u)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    return u


def add_manual_listing(url: str, company: str = "", title: str = "", *, source: str = "web") -> dict:
    """Add a posting to the queue. Refuses a posting already in the queue or already applied to.
    A posting the scraper saw and screened out becomes a queued job (Surya's choice overrides triage)."""
    u = _clean_url(url)
    if not u:
        return {"ok": False, "reason": "invalid_url", "message": "That is not a valid http(s) URL."}
    key = ledger.url_key(u)
    host = urlparse(u).netloc.lower().removeprefix("www.")
    company = (company or "").strip() or host.split(".")[0].capitalize()
    title = (title or "").strip() or "(title unknown)"
    ats = triage.detect_ats(u)
    lane = triage.lane_for(ats)

    con = db.connect()
    manual_id = "manual:" + hashlib.sha1(key.encode()).hexdigest()[:12]
    # Same posting already known? Compare canonical URL keys (host + path, no query, no trailing /apply).
    path_hint = key.rsplit("/", 1)[-1] or host
    for row in con.execute("SELECT l.id, l.url, l.company, l.title, j.id AS job_id, j.status FROM listings l "
                           "LEFT JOIN jobs j ON j.listing_id=l.id WHERE l.id = ? OR l.url LIKE ?",
                           (manual_id, f"%{path_hint}%")).fetchall():
        if row["id"] != manual_id and ledger.url_key(row["url"] or "") != key:
            continue
        if row["job_id"] is not None:
            return {"ok": False, "reason": "duplicate", "job_id": row["job_id"], "state": row["status"],
                    "message": f"Already in the queue as job #{row['job_id']} ({row['status']}): {row['company']}, {row['title']}."}
        listing_id = row["id"]
        break
    else:
        listing_id = None
    if ledger.already_applied(company if company and title != "(title unknown)" else "", title, u):
        return {"ok": False, "reason": "already_applied", "message": "Already applied to this posting (application ledger)."}

    ts = db.now()
    with db.tx() as con:
        if listing_id is None:
            listing_id = manual_id
            con.execute(
                "INSERT OR IGNORE INTO listings(id,company,title,url,category,locations,terms,degrees,sponsorship,source,"
                "date_posted,date_updated,first_seen,updated_at,ats,variant,triage_status,triage_reason,active)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (listing_id, company, title, u, "", "[]", "[]", "[]", "", "manual", ts, ts, ts, ts, ats, "swe",
                 "queued", f"added from the {source}"))
        else:
            con.execute("UPDATE listings SET triage_status='queued', triage_reason=?, updated_at=? WHERE id=?",
                        (f"added from the {source}", ts, listing_id))
        cur = con.execute(
            "INSERT INTO jobs(listing_id,status,lane,priority,attempts,apply_url,resume_variant,created_at,updated_at)"
            " VALUES(?,?,?,?,0,?,?,?,?)",
            (listing_id, states.QUEUED, lane, MANUAL_PRIORITY, u, "swe", ts, ts))
        job_id = int(cur.lastrowid)
    from .config import SETTINGS
    runnable = lane in (SETTINGS.lanes_enabled or ["no_account"])
    db.event(job_id, "listing:added", {"listing_id": listing_id, "company": company, "title": title, "ats": ats,
                                       "lane": lane, "by": source, "runnable": runnable})
    msg = f"Queued job #{job_id}: {company}, {title} ({ats})."
    if not runnable:
        msg += f" The {lane or 'manual'} lane is off on the agent, so it waits until LANES_ENABLED includes it."
    return {"ok": True, "job_id": job_id, "listing_id": listing_id, "ats": ats, "lane": lane,
            "runnable": runnable, "message": msg}
