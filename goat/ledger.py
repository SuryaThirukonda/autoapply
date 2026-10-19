"""Never apply twice. Canonical keys ported from Hermes (sweeplib.url_key / applied_keys /
already_applied) so the old `applied` table imports without translation."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from . import db
from .answers import norm

_LOOSE_DROP = re.compile(r"\([^)]*\)|\b(summer|fall|spring|winter)\b|\b20\d\d\b|\bremote\b|\bearly careers?\b")


# Query parameters that name the posting itself. Without them every Greenhouse embed link
# ("boards.greenhouse.io/embed/job_app?token=8175510") and every careers page that carries ?gh_jid= had ONE key:
# intake attached the Coinbase sheet row to Simplify's FanDuel listing, and Figma's ledger key made every
# job-boards.greenhouse.io embed posting look applied to (2026-10-19).
ID_PARAMS = ("token", "gh_jid", "jobid", "job_id", "jid", "id", "reqid", "req_id", "requisitionid", "jobreqid",
             "positionid", "jobpostingid", "postingid")


def url_key(url: str) -> str:
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return ""
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (p.path or "").rstrip("/").lower()
    # the apply pages of a posting are the posting: Workday's /apply/applyManually, /apply/autofillWithResume
    path = re.sub(r"/(apply|application)(/[a-z0-9_-]+)?$", "", path)
    ids = []
    for k, vals in sorted(parse_qs(p.query or "").items()):
        if k.lower() in ID_PARAMS and vals and vals[0].strip():
            ids.append(f"{k.lower()}={vals[0].strip().lower()}")
    return (f"{host}{path}" + ("?" + "&".join(ids) if ids else "")) if host else ""


def applied_keys(company: str, title: str, url: str) -> list[str]:
    keys = []
    uk = url_key(url)
    if uk:
        keys.append("u:" + uk)
    ck, tk = norm(company), norm(title)
    if ck and tk:
        keys.append(f"c:{ck}|{tk}")
    return keys


def loose_title(title: str) -> str:
    t = (title or "").lower()
    t = _LOOSE_DROP.sub(" ", t)
    t = t.replace("engineering", "engineer").replace("internship", "intern")
    return " ".join(re.findall(r"[a-z0-9+#]+", t))


# A posting's id in its link or title: Workday "_R69136" / "(R68679)", "REQ-12794", "JR10261", Oracle "job/260818",
# or any 7+ digit number (Greenhouse 8175510, gh_jid=8193731).
_POSTING_ID = re.compile(r"(?<![a-z0-9])(?:r|req|jr|job|requisition)[-_ /]?(\d{4,})|(\d{7,})", re.I)


def posting_ids(*texts: str) -> set[str]:
    out: set[str] = set()
    for t in texts:
        for m in _POSTING_ID.finditer(t or ""):
            out.add((m.group(1) or m.group(2)).lstrip("0") or "0")
    return out


def _host(key: str) -> str:
    return (key or "").split("/", 1)[0].split("?", 1)[0]


def already_applied(company: str, title: str, url: str) -> bool:
    """The same posting is in the ledger: its link, its posting id, or the same company and title unless both links
    carry posting ids that differ. Two postings of one company that share a title but name different ids are
    different applications: Northrop Grumman's Aurora and Colorado Springs "Software Engineer
    Intern" postings were one key, so after one was submitted the other's gate said "already applied" (2026-10-19)."""
    con = db.connect()
    uk = url_key(url)
    if uk and con.execute("SELECT 1 FROM applied_keys WHERE key=?", ("u:" + uk,)).fetchone():
        return True
    ck = norm(company)
    if not ck:
        return False
    ids = posting_ids(url, title)
    lt = loose_title(title)
    exact_c = f"c:{ck}|{norm(title)}" if norm(title) else None
    for row in con.execute("SELECT key, company, title, url FROM applied_keys"):
        if norm(row["company"]) != ck:
            continue
        row_ids = posting_ids(row["url"] or "", row["title"] or "")
        if ids & row_ids:
            return True       # the same posting id
        if ids and row_ids:
            continue          # both name their posting and the ids differ (Motorola R69136 vs the tracker's R68679)
        other = url_key(row["url"] or "")
        if uk and other and other != uk and _host(other) == _host(uk):
            continue          # one portal names them by different links: different postings
        if (exact_c and row["key"] == exact_c) or (lt and loose_title(row["title"]) == lt):
            return True
    return False


def company_application_count(company: str, exclude_listing: str | None = None) -> int:
    """Earlier applications to this company (goat's own plus the imported Hermes ledger)."""
    ck = norm(company)
    if not ck:
        return 0
    con = db.connect()
    ids = set()
    for row in con.execute("SELECT id, company, listing_id FROM applications"):
        if norm(row["company"]) == ck and not (exclude_listing and row["listing_id"] == exclude_listing):
            ids.add(row["id"])
    for row in con.execute("SELECT application_id, company FROM applied_keys"):
        if norm(row["company"]) == ck and row["application_id"] not in ids:
            app = con.execute("SELECT listing_id FROM applications WHERE id=?", (row["application_id"],)).fetchone()
            if not (exclude_listing and app and app["listing_id"] == exclude_listing):
                ids.add(row["application_id"])
    return len(ids)


def record_application(listing_id: str | None, company: str, title: str, url: str, origin: str,
                       signal: str = "", submitted_at: int | None = None) -> int:
    """Insert the application and all its keys. Idempotent on the canonical url key."""
    ck = "u:" + url_key(url) if url_key(url) else f"c:{norm(company)}|{norm(title)}"
    with db.tx() as con:
        row = con.execute("SELECT id FROM applications WHERE canonical_key=?", (ck,)).fetchone()
        if row:
            app_id = row["id"]
        else:
            cur = con.execute(
                "INSERT INTO applications(listing_id,canonical_key,company,title,url,origin,submitted_at,signal)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (listing_id, ck, company, title, url, origin, submitted_at or db.now(), signal))
            app_id = cur.lastrowid
        for k in applied_keys(company, title, url):
            con.execute(
                "INSERT OR IGNORE INTO applied_keys(key,application_id,company,title,url,origin,added_at)"
                " VALUES(?,?,?,?,?,?,?)", (k, app_id, company, title, url, origin, db.now()))
    return app_id


def mark_confirmed(application_id: int, email_id: str) -> None:
    with db.tx() as con:
        con.execute("UPDATE applications SET confirmation_email_id=?, confirmed_at=? WHERE id=?",
                    (email_id, db.now(), application_id))


def unconfirmed_older_than(seconds: int) -> list:
    cutoff = db.now() - seconds
    return db.connect().execute(
        "SELECT * FROM applications WHERE confirmed_at IS NULL AND submitted_at < ? ORDER BY submitted_at",
        (cutoff,)).fetchall()
