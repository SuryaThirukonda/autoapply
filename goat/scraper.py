"""SimplifyJobs listings.json (and two other compilations) -> listings -> jobs, plus best-effort posting liveness checks.

The scraper is the only writer of `listings`. The first pass over the feed is a *baseline*: every
row is recorded with the triage status it would have received (reason "would_be:<st>:<reason>")
and no job is created, so the 17k historical rows never flood the queue. `backfill` can later
promote recent baseline rows that would have been queued.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from . import db, ledger, states, triage
from .answers import norm
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


SKIPPED_PORTAL_REASON = "skipped: {ats} is not run (SKIP_ATS; iCIMS needs a CAPTCHA at account creation)"


def _insert_job(con, listing_id: str, *, ats: str, variant: str, url: str, priority: int = 0,
                created_at: int | None = None) -> int:
    ts = db.now()
    # a portal the worker never claims (SKIP_ATS) gets its job closed at once, so it never sits in the queue
    skipped = (ats or "").lower() in [a.lower() for a in (SETTINGS.skip_ats or [])]
    cur = con.execute(
        "INSERT INTO jobs(listing_id,status,lane,priority,attempts,apply_url,resume_variant,created_at,updated_at,hold_reason,finished_at)"
        " VALUES(?,?,?,?,0,?,?,?,?,?,?)",
        (listing_id, states.DEAD if skipped else states.QUEUED, triage.lane_for(ats), priority, url, variant or "swe",
         created_at or ts, ts, SKIPPED_PORTAL_REASON.format(ats=ats) if skipped else None, ts if skipped else None))
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
    held = None     # listings that would have a job, read when the first new one comes
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
            if status in (triage.QUEUED, triage.MANUAL):
                # one job per posting: Simplify listing a posting another compilation already gave goat adds nothing
                if held is None:
                    held = PostingIndex(con.execute(
                        "SELECT id, company, title, url FROM listings WHERE triage_status IN ('queued', 'manual')"))
                dup = held.find(company, title, url, other_than=feed_of(lid))
                if dup:
                    status, reason = triage.SKIPPED, f"duplicate:{dup}"
                    counts["duplicate"] = counts.get("duplicate", 0) + 1
                else:
                    held.add(lid, company, title, url)
            con.execute(_INSERT_LISTING, _listing_values(
                row, ats=ats, variant=variant, triage_status=status, triage_reason=reason, ts=ts))
            fit = triage.role_fit(title)
            if status == triage.QUEUED:
                job_id = _insert_job(con, lid, ats=ats, variant=variant, url=url)
                if fit == triage.ALIGNED:
                    counts["aligned"] = counts.get("aligned", 0) + 1     # worker.rank_queue puts these first
            counts[status] = counts.get(status, 0) + 1
            _event(con, job_id, "listing:new", {"listing_id": lid, "status": status, "reason": reason,
                                                "ats": ats, "company": company, "title": title, "role_fit": fit})
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
        if extra_feeds_on():
            try:
                extra = await asyncio.to_thread(poll_extra_once)
                if any(isinstance(v, dict) and (v.get("new") or v.get("error")) for v in extra.values()):
                    log.info("other compilations: %s", json.dumps(extra, sort_keys=True))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - Simplify's feed goes on whatever happens here
                log.warning("other compilations failed: %s: %s", type(e).__name__, e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=SETTINGS.scrape_interval_s)
        except asyncio.TimeoutError:
            continue


# --------------------------------------------------------------------------- other compilations

# Two compilations besides Simplify (Surya, 2026-10-22: "Find new listing sites and internship compilations to queue"):
# zshah101's automated list scrapes company boards directly and gave goat 152 aligned roles Simplify never listed;
# adminazhar's lists link each posting's own form and gave 35 more. vanshb03's list held nothing Simplify lacked, and
# jobright's lists link only to jobright.ai, not to the employer's form. Only open internships in his aligned roles in
# the US are taken from them, by the title rules the first hand-checked batch used, and a posting goat already holds
# (ledger.same_posting: the same link, posting id or title) is left out. GOAT_EXTRA_FEEDS=0 turns them off.
EXTRA_FEEDS = (
    ("zshah101", "https://raw.githubusercontent.com/zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships/main/data/jobs.json"),
    ("adminazhar", "https://raw.githubusercontent.com/adminazhar/Summer-2027-Internships/main/README.md"),
    ("adminazhar", "https://raw.githubusercontent.com/adminazhar/Summer-2027-Internships/main/ENGINEERING_INTERNSHIPS.md"),
)
# which feed gave goat a listing, by its id ('manual:' rows come from the tracker sheet, the rest from Simplify)
FEED_PREFIXES = {"zshah-": "zshah101", "adminazhar-": "adminazhar", "manual:": "manual"}
_INTERNSHIP = re.compile(r"\b(intern|internship|co-?op|summer (analyst|associate)|apprentice(ship)?)\b", re.I)
_US_STATES = ("AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|"
              "OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC|PR")
_US_STATE_NAMES = ("alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|"
                   "illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
                   "mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|"
                   "north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|"
                   "south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming|"
                   "district of columbia|puerto rico")
# 'Austin, TX', 'Atlanta GA', 'US-IA-Cedar Rapids', 'Boston, Massachusetts', 'United States', 'Remote', 'Multiple'
_US_PLACE = re.compile(r"(?<![A-Za-z])(%s)(?![A-Za-z])|\bUSA?\b|U\.S\.|(?i:\b(%s|united states|remote|multiple|nyc|"
                       r"san francisco|bay area|silicon valley|seattle|chicago|boston|los angeles)\b)"
                       % (_US_STATES, _US_STATE_NAMES))
# another country (named or by its ISO code), a Canadian province, or a city abroad whose short form looks like a US
# state ('Hyderabad, IN', 'Berlin, DE', 'Tel Aviv, IL')
_ABROAD = re.compile(r"\b(canada|india|united kingdom|uk|england|scotland|ireland|germany|france|china|japan|singapore|"
                     r"poland|brazil|israel|australia|netherlands|spain|taiwan|korea|philippines|costa rica|hungary|"
                     r"romania|czech|vietnam|hong kong|switzerland|sweden|denmark|italy|portugal|argentina|chile|colombia|"
                     r"egypt|uae|serbia|(?<!new )mexico|ind|chn|gbr|deu|ita|jpn|sgp|isr|pol|fra|esp|nld|irl|aus|bra|mex|kor|"
                     r"twn|phl|rou|hun|cze|toronto|montreal|ottawa|vancouver(?!,?\s*(wa|washington)\b)|bengaluru|"
                     r"bangalore|hyderabad|pune|chennai|mumbai|noida|gurgaon|gurugram|beijing|shanghai|shenzhen|tokyo|"
                     r"seoul|taipei|berlin|munich|frankfurt|hamburg|krakow|tel aviv|haifa|herzliya|sydney|zurich|"
                     r"amsterdam|prague|bucharest|budapest|manila|guadalajara|sao paulo|buenos aires|bogota)\b"
                     r"|,\s*(ON|BC|QC|AB|MB|SK|NS|NB|NL|PE)\b", re.I)
# titles the first hand-checked batch left out: other engineering fields, graduate-only roles, customer-facing chip
# roles, epidemiology and strategy work, civil infrastructure design
_NOT_HIS = re.compile(r"\b(mechanical|civil|electrical|chemical|manufacturing|hardware|structural|aerospace engineering|"
                      r"materials|process engineer|mechatronics|graduate intern|ph\.?d|master'?s|mba|"
                      r"field applications? engineer|applications engineer(ing)?|equipment engineer|yield technology|"
                      r"physical systems|epidemiology|strategy and m&a|bridge design|roadway design|"
                      r"transportation (planning|engineering)|water infrastructure)\b", re.I)
# quant and trading firms are not his direction (Simplify files their roles under its Quant category, which triage
# rejects; these lists file them under Software)
_QUANT_FIRM = re.compile(r"\b(trading|traders|optiver|jane street|citadel|hudson river|two sigma|d\.? ?e\.? shaw|"
                         r"susquehanna|sig(?! sauer)|imc|jump|five rings|tower research|akuna|drw|virtu|old mission|"
                         r"belvedere|wolverine|peak6|radix|xtx|squarepoint|millennium management|balyasny|schonfeld|"
                         r"point72|renaissance technologies|headlands|maven securities|da vinci|g-research|qube|"
                         r"trexquant|aquatic capital|arrowstreet|voleon|cubist|engineers gate|bridgewater associates|"
                         r"man group|capital fund management)\b", re.I)
_DATA_TITLE = re.compile(r"\b(data|analytics?|analyst|machine learning|ml|ai|artificial intelligence|scien(ce|tist))\b", re.I)


def extra_feeds_on() -> bool:
    return os.environ.get("GOAT_EXTRA_FEEDS", "1") != "0"


def feed_of(listing_id: str) -> str:
    return next((name for prefix, name in FEED_PREFIXES.items() if str(listing_id).startswith(prefix)), "simplify")


class PostingIndex:
    """Listings matched the way the ledger matches an application: the same link, or one company's posting with the
    same posting id or title (ledger.same_posting)."""

    def __init__(self, rows=()):
        self._links: dict[str, list[str]] = {}
        self._companies: dict[str, list[tuple[str, str, str]]] = {}
        self._roles: dict[tuple[str, str], str] = {}
        for lid, company, title, url in rows:
            self.add(lid, company, title, url)

    def add(self, lid: str, company: str, title: str, url: str) -> None:
        uk = ledger.url_key(url)
        if uk:
            self._links.setdefault(uk, []).append(lid)
        if norm(company):
            self._companies.setdefault(norm(company), []).append((lid, title or "", url or ""))
            self._roles.setdefault((norm(company), ledger.loose_title(title)), lid)

    def find(self, company: str, title: str, url: str, other_than: str | None = None) -> str | None:
        """The id of a listing that is this posting (one another feed than `other_than` gave goat), else None."""
        uk = ledger.url_key(url)
        for lid in self._links.get(uk, []) if uk else []:
            if other_than is None or feed_of(lid) != other_than:
                return lid
        for lid, t, u in self._companies.get(norm(company), []):
            if (other_than is None or feed_of(lid) != other_than) and ledger.same_posting(title, url, t, u):
                return lid
        return None

    def same_role(self, company: str, title: str) -> str | None:
        """The id of a listing for this company's role of the same title, whatever its posting id or place."""
        return self._roles.get((norm(company), ledger.loose_title(title))) if norm(company) else None


def _iso_epoch(v: Any) -> int | None:
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()) if v else None
    except ValueError:
        return None


def _category(title: str) -> str:
    return "AI/ML/Data" if _DATA_TITLE.search(title or "") else "Software"


def zshah_rows(payload: Any) -> list[dict]:
    """zshah101 data/jobs.json ({id: job} with company, title, url, location, posted_at, season(s), is_open) in
    Simplify's row shape."""
    items = payload.values() if isinstance(payload, dict) else (payload or [])
    out = []
    for r in items:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        title = r.get("title") or ""
        out.append({"id": f"zshah-{r['id']}", "company_name": r.get("company") or "", "title": title, "url": r.get("url") or "",
                    "category": _category(title), "locations": [x.strip() for x in (r.get("location") or "").split(";") if x.strip()],
                    "terms": r.get("seasons") or ([r["season"]] if r.get("season") else []), "degrees": [],
                    "sponsorship": r.get("sponsorship") or "", "source": "zshah101", "date_posted": _iso_epoch(r.get("posted_at")),
                    "date_updated": _iso_epoch(r.get("last_seen_at")), "active": bool(r.get("is_open")), "is_visible": True})
    return out


def _md_plain(cell: str) -> str:
    return re.sub(r"<[^>]+>|\*\*|\[|\]\([^)]*\)", "", cell).strip()


def adminazhar_rows(md: str, now: int | None = None) -> list[dict]:
    """adminazhar's table rows | Company | Role | Location | Pay | Age | Apply links | in Simplify's row shape; the apply
    link is the employer's own (never the talentdpro mirror)."""
    now = now or int(time.time())
    out = []
    for line in (md or "").splitlines():
        if not line.startswith("|") or "href=" not in line:
            continue
        c = [x.strip() for x in line.strip().strip("|").split("|")]
        if len(c) < 6:
            continue
        links = [u for u in re.findall(r'href="([^"]+)"', c[5]) if "talentdpro.com" not in u]
        if not links:
            continue
        age = re.match(r"(\d+)\s*d", c[4] or "")
        title = _md_plain(c[1])
        locs = [x for x in (_md_plain(p) for p in re.split(r"<br\s*/?>|</br>|;", c[2])) if x]
        out.append({"id": "adminazhar-" + hashlib.sha1(links[0].encode()).hexdigest()[:16], "company_name": _md_plain(c[0]),
                    "title": title, "url": links[0], "category": _category(title), "locations": locs,
                    "terms": [], "degrees": [], "sponsorship": "", "source": "adminazhar",
                    "date_posted": now - int(age.group(1)) * 86400 if age else None, "date_updated": now,
                    "active": True, "is_visible": True})
    return out


def us_place(location: str) -> bool:
    """A US state ('Austin, TX'), the US named, Remote or Multiple, and no other country or city abroad."""
    return bool(_US_PLACE.search(location or "")) and not _ABROAD.search(location or "")


def keep_extra(row: dict) -> bool:
    """An open internship in one of his aligned roles and fields, in the US, at no quant or trading firm."""
    title = row.get("title") or ""
    locs = [str(x) for x in (row.get("locations") or [])]
    return (bool(row.get("active", True)) and bool(_INTERNSHIP.search(title)) and triage.role_fit(title) == triage.ALIGNED
            and not _NOT_HIS.search(title) and not _QUANT_FIRM.search(row.get("company_name") or "")
            and (not locs or any(us_place(x) for x in locs)))


def _fetch_feed(label: str, url: str) -> str | None:
    """The feed's text, or None when GitHub says it has not changed since the last read."""
    key = f"extra_feed:{label}:etag"
    headers = {"User-Agent": "goat-scraper/1.0"}
    etag = db.meta_get(key)
    if etag:
        headers["If-None-Match"] = etag
    with httpx.Client(timeout=90, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)
    if resp.status_code == 304:
        return None
    resp.raise_for_status()
    if resp.headers.get("etag"):
        db.meta_set(key, resp.headers["etag"])
    return resp.text


def poll_extra_once(feeds=EXTRA_FEEDS, fetch=None) -> dict:
    """Read each other compilation and ingest its new, kept rows. Rows goat already holds from that feed go through
    too, so a row zshah101 marks closed closes its listing and job. A feed that cannot be read is skipped this time."""
    fetch = fetch or _fetch_feed
    out: dict = {}
    con = db.connect()
    held = PostingIndex(con.execute("SELECT id, company, title, url FROM listings"))
    for name, url in feeds:
        label = f"{name}:{url.rsplit('/', 1)[-1]}"
        try:
            text = fetch(label, url)
            if text is None:
                out[label] = {"status": 304}
                continue
            rows = zshah_rows(json.loads(text)) if name == "zshah101" else adminazhar_rows(text)
        except Exception as e:  # noqa: BLE001 - a feed that cannot be read is skipped this time
            out[label] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
            continue
        known = {r[0] for r in con.execute("SELECT id FROM listings WHERE source=?", (name,))}
        batch = []
        for r in rows:
            if r["id"] in known:
                batch.append(r)
            # one row per company and title: the per-site copies of one role (Lockheed Martin listed one 36 times)
            # are left out, as in the first hand-checked batch
            elif keep_extra(r) and not (held.find(r["company_name"], r["title"], r["url"])
                                        or held.same_role(r["company_name"], r["title"])):
                held.add(r["id"], r["company_name"], r["title"], r["url"])
                batch.append(r)
        out[label] = ingest(batch, first_run=False) if batch else {"new": 0}
        db.meta_set(f"extra_feed:{label}:last_ok", db.now())
    return out


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
