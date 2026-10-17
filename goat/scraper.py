"""SimplifyJobs listings.json -> listings -> jobs, plus best-effort posting liveness checks.

The scraper is the only writer of `listings`. The first pass over the feed is a *baseline*: every
row is recorded with the triage status it would have received (reason "would_be:<st>:<reason>")
and no job is created, so the 17k historical rows never flood the queue. `backfill` can later
promote recent baseline rows that would have been queued.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from . import db, ledger, states, triage
from .config import SETTINGS

log = logging.getLogger("goat.scraper")

CLOSEABLE = ("queued", "manual", "unsure")

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


# --------------------------------------------------------------------------- fetch

def fetch_listings(etag: str | None) -> tuple[int, list | None, str | None]:
    """GET the listings feed with If-None-Match. Returns (status, rows, etag); rows is None on 304.
    Raises httpx.HTTPStatusError on anything other than 200/304."""
    headers = {"Accept": "application/json", "User-Agent": "goat-scraper/1.0"}
    if etag:
        headers["If-None-Match"] = etag
    with httpx.Client(timeout=90, follow_redirects=True) as client:
        resp = client.get(SETTINGS.listings_url, headers=headers)
    if resp.status_code == 304:
        return 304, None, etag
    resp.raise_for_status()
    rows = resp.json()
    if not isinstance(rows, list):
        raise ValueError(f"listings feed is not a JSON list ({type(rows).__name__})")
    return resp.status_code, rows, resp.headers.get("etag") or etag


# --------------------------------------------------------------------------- ingest

def _is_active(row: dict) -> bool:
    return bool(row.get("active", True)) and bool(row.get("is_visible", True))


def _as_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _listing_values(row: dict, *, ats: str, variant: str, triage_status: str, triage_reason: str,
                    ts: int) -> tuple:
    return (
        str(row["id"]),
        row.get("company_name") or row.get("company") or "",
        row.get("title") or "",
        row.get("url") or "",
        row.get("category") or "",
        json.dumps(row.get("locations") or []),
        json.dumps(row.get("terms") or []),
        json.dumps(row.get("degrees") or []),
        row.get("sponsorship") or "",
        row.get("source") or "",
        _as_int(row.get("date_posted")),
        _as_int(row.get("date_updated")),
        ts, ts,
        ats, variant,
        triage_status, triage_reason,
        1 if _is_active(row) else 0,
    )


_INSERT_LISTING = (
    "INSERT INTO listings(id,company,title,url,category,locations,terms,degrees,sponsorship,source,"
    "date_posted,date_updated,first_seen,updated_at,ats,variant,triage_status,triage_reason,active)"
    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _insert_job(con, listing_id: str, *, ats: str, variant: str, url: str, priority: int = 0,
                created_at: int | None = None) -> int:
    ts = db.now()
    cur = con.execute(
        "INSERT INTO jobs(listing_id,status,lane,priority,attempts,apply_url,resume_variant,created_at,updated_at)"
        " VALUES(?,?,?,?,0,?,?,?,?)",
        (listing_id, states.QUEUED, triage.lane_for(ats), priority, url, variant or "swe",
         created_at or ts, ts))
    return cur.lastrowid


def _event(con, job_id: int | None, kind: str, detail: Any) -> None:
    con.execute("INSERT INTO events(job_id,ts,kind,detail) VALUES(?,?,?,?)",
                (job_id, db.now(), kind, json.dumps(db.redact(detail), default=str)))


def ingest(rows: list[dict], *, first_run: bool) -> dict:
    """Diff the feed against `listings`. One transaction for the whole batch."""
    counts = {"seen": 0, "new": 0, "baseline": 0, "queued": 0, "manual": 0, "unsure": 0,
              "rejected": 0, "skipped": 0, "already_applied": 0, "closed": 0, "jobs_closed": 0,
              "no_id": 0}
    ts = db.now()
    with db.tx() as con:
        for row in rows:
            lid = row.get("id")
            if not lid:
                counts["no_id"] += 1
                continue
            lid = str(lid)
            counts["seen"] += 1
            existing = con.execute(
                "SELECT triage_status, active FROM listings WHERE id=?", (lid,)).fetchone()
            if existing is not None:
                if not _is_active(row) and existing["triage_status"] in CLOSEABLE:
                    con.execute(
                        "UPDATE listings SET active=0, triage_status='closed', updated_at=?, "
                        "date_updated=COALESCE(?, date_updated) WHERE id=?",
                        (ts, _as_int(row.get("date_updated")), lid))
                    counts["closed"] += 1
                    job = con.execute(
                        "SELECT id, status FROM jobs WHERE listing_id=? AND status IN (?,?)",
                        (lid, states.QUEUED, states.HOLD)).fetchone()
                    if job is not None:
                        con.execute(
                            "UPDATE jobs SET status=?, hold_reason='posting_closed', updated_at=?, "
                            "finished_at=? WHERE id=?", (states.DEAD, ts, ts, job["id"]))
                        _event(con, job["id"], "job:dead", {"reason": "posting_closed", "listing_id": lid})
                        counts["jobs_closed"] += 1
                continue

            status, reason, ats, variant = triage.classify(row)
            counts["new"] += 1
            company = row.get("company_name") or row.get("company") or ""
            title = row.get("title") or ""
            url = row.get("url") or ""
            if first_run:
                con.execute(_INSERT_LISTING, _listing_values(
                    row, ats=ats, variant=variant, triage_status="baseline",
                    triage_reason=f"would_be:{status}:{reason}", ts=ts))
                counts["baseline"] += 1
                _event(con, None, "listing:new", {"listing_id": lid, "status": "baseline",
                                                  "would_be": status, "reason": reason, "ats": ats})
                continue

            job_id = None
            if status == triage.QUEUED:
                if ledger.already_applied(company, title, url):
                    status, reason = triage.SKIPPED, "already_applied"
                    counts["already_applied"] += 1
            con.execute(_INSERT_LISTING, _listing_values(
                row, ats=ats, variant=variant, triage_status=status, triage_reason=reason, ts=ts))
            if status == triage.QUEUED:
                job_id = _insert_job(con, lid, ats=ats, variant=variant, url=url)
            counts[status] = counts.get(status, 0) + 1
            _event(con, job_id, "listing:new", {"listing_id": lid, "status": status, "reason": reason,
                                                "ats": ats, "company": company, "title": title})
    return counts


# --------------------------------------------------------------------------- backfill

def backfill(days: int, dry_run: bool) -> list[dict]:
    """Promote baseline listings posted within `days` days whose would-be status was queued."""
    cutoff = db.now() - int(days) * 86400
    out: list[dict] = []
    with db.tx() as con:
        rows = con.execute(
            "SELECT l.* FROM listings l LEFT JOIN jobs j ON j.listing_id = l.id "
            "WHERE l.triage_status='baseline' AND l.triage_reason LIKE 'would_be:queued:%' "
            "AND l.active=1 AND l.date_posted IS NOT NULL AND l.date_posted >= ? AND j.id IS NULL "
            "ORDER BY l.date_posted DESC", (cutoff,)).fetchall()
        for r in rows:
            if ledger.already_applied(r["company"], r["title"], r["url"]):
                if not dry_run:
                    con.execute("UPDATE listings SET triage_status='skipped', triage_reason='already_applied', "
                                "updated_at=? WHERE id=?", (db.now(), r["id"]))
                continue
            item = {"listing_id": r["id"], "company": r["company"], "title": r["title"], "url": r["url"],
                    "ats": r["ats"], "lane": triage.lane_for(r["ats"]), "date_posted": r["date_posted"]}
            if not dry_run:
                job_id = _insert_job(con, r["id"], ats=r["ats"], variant=r["variant"], url=r["url"])
                con.execute("UPDATE listings SET triage_status='queued', triage_reason='', updated_at=? "
                            "WHERE id=?", (db.now(), r["id"]))
                _event(con, job_id, "listing:backfill", {"listing_id": r["id"], "days": days})
                item["job_id"] = job_id
            out.append(item)
    return out


# --------------------------------------------------------------------------- loop

def poll_once() -> dict:
    """One fetch+ingest with meta bookkeeping. Raises on fetch failure (the loop counts it)."""
    etag = db.meta_get("etag")
    status, rows, new_etag = fetch_listings(etag)
    if status == 304 or rows is None:
        db.meta_set("last_poll_ok", db.now())
        return {"status": 304}
    first_run = db.meta_get("baseline_done") is None
    counts = ingest(rows, first_run=first_run)
    counts["status"] = status
    counts["first_run"] = first_run
    db.meta_set("etag", new_etag)
    if first_run:
        db.meta_set("baseline_done", db.now())
    db.meta_set("listing_count", len(rows))
    db.meta_set("last_poll_ok", db.now())
    db.meta_set("fetch_failures", 0)
    return counts


async def scrape_loop(stop: asyncio.Event) -> None:
    """Poll the feed every SETTINGS.scrape_interval_s until `stop` is set. Never raises."""
    while not stop.is_set():
        try:
            result = await asyncio.to_thread(poll_once)
            if result.get("status") == 304:
                log.info("listings unchanged (304)")
            else:
                log.info("listings ingested: %s", json.dumps(result, sort_keys=True))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - the supervisor must survive anything here
            try:
                n = int(db.meta_get("fetch_failures", 0) or 0) + 1
                db.meta_set("fetch_failures", n)
            except Exception:  # noqa: BLE001
                n = -1
            log.warning("scrape failed (%d consecutive): %s: %s", n, type(e).__name__, e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=SETTINGS.scrape_interval_s)
        except asyncio.TimeoutError:
            continue


# --------------------------------------------------------------------------- liveness

def parse_greenhouse(url: str) -> tuple[str, str] | None:
    """(board_token, job_id) from job-boards.greenhouse.io/<board>/jobs/<id>,
    boards.greenhouse.io/<board>/jobs/<id>, boards.greenhouse.io/embed/job_app?for=<board>&token=<id>,
    or any URL carrying ?gh_jid=<id> (board = first path segment or `for` param, if any)."""
    try:
        p = urlparse(url or "")
    except ValueError:
        return None
    host = (p.netloc or "").lower()
    qs = parse_qs(p.query or "")
    parts = [x for x in (p.path or "").split("/") if x]
    if host.endswith("greenhouse.io"):
        m = re.match(r"^/([^/]+)/jobs/(\d+)", p.path or "")
        if m:
            return m.group(1), m.group(2)
        if parts and parts[0] == "embed" and qs.get("for") and qs.get("token"):
            return qs["for"][0], qs["token"][0]
        if qs.get("gh_jid") and parts:
            return parts[0], qs["gh_jid"][0]
        return None
    if qs.get("gh_jid"):
        # Embedded board on a company site: the board token is usually the company slug, which
        # the host carries (sagesure.com -> "sagesure"). A guess: callers treat 404 as unknown.
        board = (qs.get("for") or [None])[0]
        if not board:
            labels = [x for x in host.split(":")[0].split(".") if x and x != "www"]
            if len(labels) >= 2:
                board = labels[-2]
        if board:
            return board, qs["gh_jid"][0]
    return None


def parse_lever(url: str) -> tuple[str, str] | None:
    """(company, posting_uuid) from jobs.lever.co/<company>/<uuid>[/apply]."""
    try:
        p = urlparse(url or "")
    except ValueError:
        return None
    if not (p.netloc or "").lower().endswith("lever.co"):
        return None
    m = re.match(rf"^/([^/]+)/({_UUID})", p.path or "")
    return (m.group(1), m.group(2).lower()) if m else None


def parse_ashby(url: str) -> tuple[str, str] | None:
    """(org, posting_uuid) from jobs.ashbyhq.com/<org>/<uuid>[/application]."""
    try:
        p = urlparse(url or "")
    except ValueError:
        return None
    if not (p.netloc or "").lower().endswith("ashbyhq.com"):
        return None
    m = re.match(rf"^/([^/]+)/({_UUID})", p.path or "")
    return (m.group(1), m.group(2).lower()) if m else None


def workday_api_url(url: str) -> str | None:
    """https://<t>.wd1.myworkdayjobs.com/en-US/<site>/job/<loc>/<slug> -> the cxs JSON endpoint for that job."""
    try:
        u = urlparse(url)
    except ValueError:
        return None
    if "myworkdayjobs.com" not in u.netloc:
        return None
    parts = [x for x in u.path.split("/") if x]
    if parts and re.match(r"^[a-z]{2}-[A-Z]{2}$", parts[0]):
        parts = parts[1:]
    if len(parts) < 3 or "job" not in parts:
        return None
    while parts and parts[-1] in ("apply", "applyManually", "autofillWithResume", "useMyLastApplication"):
        parts = parts[:-1]
    tenant = u.netloc.split(".")[0]
    return f"https://{u.netloc}/wday/cxs/{tenant}/{parts[0]}/{'/'.join(parts[1:])}"


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&#160;", " ", text).replace("&amp;", "&").replace("&#36;", "$")
    return " ".join(text.split())


def fetch_posting_text(url: str, max_chars: int = 40000) -> str:
    """Plain text of a posting page (for pay figures and requirements). '' on any failure.
    Workday pages render in JavaScript, so their description comes from the job's JSON endpoint."""
    api = workday_api_url(url)
    if api:
        try:
            r = httpx.get(api, timeout=12, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                info = (r.json() or {}).get("jobPostingInfo") or {}
                return _html_to_text(info.get("jobDescription", ""))[:max_chars]
        except Exception:
            pass
    try:
        r = httpx.get(url, timeout=12, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return ""
        return _html_to_text(r.text)[:max_chars]
    except Exception:
        return ""


def check_posting_live(ats: str, url: str) -> bool | None:
    """Best-effort liveness via keyless public read APIs. None = unknown."""
    try:
        if ats == "greenhouse":
            parsed = parse_greenhouse(url)
            if not parsed:
                return None
            board, jid = parsed
            r = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}",
                          timeout=15, follow_redirects=True)
            if r.status_code == 200:
                return True
            board_known = urlparse(url).netloc.lower().endswith("greenhouse.io")
            return False if (r.status_code == 404 and board_known) else None
        if ats == "lever":
            parsed = parse_lever(url)
            if not parsed:
                return None
            company, pid = parsed
            r = httpx.get(f"https://api.lever.co/v0/postings/{company}/{pid}", timeout=15,
                          follow_redirects=True)
            return True if r.status_code == 200 else (False if r.status_code == 404 else None)
        if ats == "ashby":
            parsed = parse_ashby(url)
            if not parsed:
                return None
            org, pid = parsed
            r = httpx.get(f"https://api.ashbyhq.com/posting-api/job-board/{org}", timeout=15,
                          follow_redirects=True)
            if r.status_code != 200:
                return None
            data = r.json()
            jobs = data.get("jobs") if isinstance(data, dict) else None
            if not isinstance(jobs, list):
                return None
            for j in jobs:
                if isinstance(j, dict) and str(j.get("id", "")).lower() == pid:
                    return True
            return pid in r.text.lower() or False
    except Exception as e:  # noqa: BLE001 - best effort only
        log.debug("liveness check failed for %s %s: %s", ats, url, e)
        return None
    return None
