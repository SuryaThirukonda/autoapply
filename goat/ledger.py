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


_GREENHOUSE_HOST = re.compile(r"^(?:job-)?boards(?:\.eu)?\.greenhouse\.io$")


def posting_key(url: str) -> str:
    """One key for the links one posting has on its own portal. Greenhouse job ids are global: boards.greenhouse.io/
    <board>/jobs/<id>?gh_jid=<id>, job-boards.greenhouse.io/<board>/jobs/<id> and the embed link's token are one posting.
    Anduril's '2027 Finance Data Analyst Intern' was applied to through one link and queued again through the other, and
    only the judge stopped a second application (2026-10-23)."""
    uk = url_key(url)
    host, _, rest = uk.partition("/")
    if _GREENHOUSE_HOST.match(host):
        m = re.search(r"/jobs/(\d+)", "/" + rest) or re.search(r"(?:token|gh_jid)=(\d+)", rest)
        if m:
            return f"greenhouse:{m.group(1)}"
    return uk


# Legal and generic words a company's name carries in one list and not in another
_COMPANY_SUFFIX = {"inc", "incorporated", "corp", "corporation", "co", "company", "llc", "ltd", "limited", "plc", "lp", "llp",
                   "group", "holdings", "the", "technologies", "technology", "tech", "systems", "solutions", "labs",
                   "laboratory", "laboratories", "services", "industries", "industry"}


def company_key(name: str) -> str:
    """One key per employer: 'Palantir Technologies' and 'Palantir', 'Qorvo, Inc.' and 'Qorvo', 'JPMorgan Chase' and
    'JP Morgan Chase', 'Anduril Industries' and 'Anduril' (parentheses, punctuation, spaces and legal or generic suffixes
    set aside). The ledger's duplicate check and the per-company cap both use it."""
    text = re.sub(r"\(.*?\)", " ", (name or "").lower())
    return "".join(t for t in re.findall(r"[a-z0-9]+", text) if t not in _COMPANY_SUFFIX)


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


def same_posting(title: str, url: str, other_title: str, other_url: str, *, same_key: bool = False) -> bool:
    """Two postings of one company are one posting: a shared posting id, or the same title (or ledger key) unless both
    name different posting ids or one portal gives them different links. The scraper matches postings two compilations
    list by it too, so each becomes one job (2026-10-22)."""
    ids, other_ids = posting_ids(url, title), posting_ids(other_url, other_title)
    if ids & other_ids:
        return True       # the same posting id
    if ids and other_ids:
        return False      # both name their posting and the ids differ (Motorola R69136 vs the tracker's R68679)
    uk, other = url_key(url), url_key(other_url)
    if uk and other and other != uk and _host(other) == _host(uk):
        return False      # one portal names them by different links: different postings
    lt = loose_title(title)
    return same_key or (bool(lt) and loose_title(other_title) == lt)


_TEST_BOARD = re.compile(r"^(test|sandbox|staging|demo)[a-z0-9-]*$")


def board_slug(url: str) -> str:
    """The employer's board name in a Greenhouse / Lever / Ashby link ('nisc' in job-boards.greenhouse.io/nisc/jobs/1)."""
    host, _, rest = url_key(url).partition("/")
    if _GREENHOUSE_HOST.match(host) or host in ("jobs.lever.co", "jobs.ashbyhq.com"):
        return rest.split("/")[0]
    return ""


def on_test_board(url: str) -> bool:
    """A board named test / sandbox / staging / demo: an employer's copy of its real board (NISC's 'testnisc', 2026-10-24)."""
    slug = board_slug(url)
    return bool(slug) and bool(_TEST_BOARD.match(slug))


def company_aliases(name: str, url: str = "") -> set[str]:
    """The keys one employer goes by: its company_key, an acronym it gives in parentheses, and its board name. 'NISC' and
    'National Information Solutions Cooperative (NISC)' were two employers, and its cybersecurity internship was applied to
    twice (2026-10-24)."""
    out = {company_key(name)}
    for inner in re.findall(r"\(([^)]+)\)", name or ""):
        k = "".join(re.findall(r"[a-z0-9]+", inner.lower()))
        if len(k) >= 3:
            out.add(k)
    slug = re.sub(r"^(test|sandbox|staging|demo)", "", board_slug(url))
    slug = re.sub(r"[^a-z0-9]", "", slug)
    if len(slug) >= 3:
        out.add(slug)
    return {k for k in out if k}


def already_applied(company: str, title: str, url: str) -> bool:
    """The same posting is in the ledger: its link, its posting id, or the same company and title unless both links
    carry posting ids that differ. Two postings of one company that share a title but name different ids are
    different applications: Northrop Grumman's Aurora and Colorado Springs "Software Engineer
    Intern" postings were one key, so after one was submitted the other's gate said "already applied" (2026-10-19)."""
    con = db.connect()
    uk = url_key(url)
    if uk and con.execute("SELECT 1 FROM applied_keys WHERE key=?", ("u:" + uk,)).fetchone():
        return True
    pk = posting_key(url) if uk else ""
    rows = con.execute("SELECT key, company, title, url FROM applied_keys").fetchall()
    if pk and pk != uk and any(posting_key(row["url"] or "") == pk for row in rows):
        return True
    mine = company_aliases(company, url)
    if not mine:
        return False
    exact_c = f"c:{norm(company)}|{norm(title)}" if norm(title) else None
    lt = loose_title(title)
    for row in rows:
        if not (company_aliases(row["company"], row["url"] or "") & mine):
            continue
        if same_posting(title, url, row["title"] or "", row["url"] or "", same_key=bool(exact_c) and row["key"] == exact_c):
            return True
        # a test board's copy of a posting carries its own id: the same title is the same job
        if (on_test_board(url) or on_test_board(row["url"] or "")) and lt and loose_title(row["title"] or "") == lt:
            return True
    return False


def company_application_count(company: str, exclude_listing: str | None = None) -> int:
    """Earlier applications to this company (goat's own plus the imported Hermes ledger)."""
    ck = company_key(company)
    if not ck:
        return 0
    con = db.connect()
    ids = set()
    for row in con.execute("SELECT id, company, listing_id FROM applications"):
        if company_key(row["company"]) == ck and not (exclude_listing and row["listing_id"] == exclude_listing):
            ids.add(row["id"])
    for row in con.execute("SELECT application_id, company FROM applied_keys"):
        if company_key(row["company"]) == ck and row["application_id"] not in ids:
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
