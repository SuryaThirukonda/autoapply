"""Import the old Hermes pipeline's SQLite data into goat's database, as DATA.

    python scripts/import_hermes.py [--hermes-dir PATH] [--dry-run]

Hermes's queue.db is opened read-only (its Chrome may still be running). Everything written is
idempotent: listings and jobs are keyed by listing id, the ledger by canonical url key, answers are
skipped when the live row already holds the same value, and events resume from the last imported n.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from goat import answers, db, ledger, states, triage  # noqa: E402
from goat.config import SETTINGS  # noqa: E402

DEFAULT_HERMES_DIR = Path(r"C:\Users\Surya\Documents\hermesagent\internship_sweep")

STATUS_MAP = {
    "baseline": "baseline", "rejected": "rejected", "skipped": "skipped", "unsure": "unsure",
    "manual": "manual", "closed": "closed",
    "queued": "queued", "hold": "queued", "in_progress": "queued", "shadow_done": "queued",
    "submitted": "submitted", "verified": "submitted",
}
JOB_STATUSES = ("queued", "hold", "in_progress", "shadow_done")
SUBMITTED_STATUSES = ("submitted", "verified")
ANSWER_SOURCES = ("standing", "bank", "policy")
EVENTS_CAP = 2000

_SECRET_Q = re.compile(r"password|passcode|passphrase|\bssn\b|social security|secret|api key|token|"
                       r"security question|pin\b|cvv|card number|routing|account number", re.I)
_TOKENISH = re.compile(r"^[A-Za-z0-9_\-+/=.]{20,}$")


def open_hermes(hermes_dir: Path) -> sqlite3.Connection:
    p = Path(hermes_dir) / "data" / "queue.db"
    if not p.exists():
        raise FileNotFoundError(p)
    uri = "file:" + p.resolve().as_posix() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def looks_secret(question: str, answer: str) -> bool:
    if _SECRET_Q.search(question or ""):
        return True
    a = (answer or "").strip()
    if not a:
        return True
    if " " in a or "@" in a or a.startswith(("http://", "https://")):
        return False
    if re.search(r"\.(pdf|docx?|txt|png|jpe?g|tex)$", a, re.I):
        return False  # a filename (resume, transcript), not a credential
    if _TOKENISH.match(a) and re.search(r"\d", a) and re.search(r"[A-Za-z]", a):
        return True
    return False


def hermes_key(question: str, prefix: str = "hermes") -> str:
    n = answers.norm(question).replace(" ", "_")[:60].rstrip("_")
    return f"{prefix}.{n or 'unknown'}"


def _live_answer_row(con, key: str):
    return con.execute(
        "SELECT answer, source, approved FROM answers WHERE key=? AND scope='global' AND scope_value='' "
        "AND superseded_by IS NULL ORDER BY id DESC LIMIT 1", (key,)).fetchone()


# --------------------------------------------------------------------------- steps

def import_listings(h: sqlite3.Connection, summary: dict) -> list[sqlite3.Row]:
    """Insert every Hermes listing. Returns the rows that are job candidates (decided later)."""
    candidates = []
    inserted = 0
    with db.tx() as con:
        for r in h.execute("SELECT * FROM listings"):
            old = r["status"] or "baseline"
            tstatus = STATUS_MAP.get(old, "baseline")
            reason = r["reason"] or ""
            ats = r["ats"] or triage.detect_ats(r["url"] or "")
            if old in JOB_STATUSES:
                candidates.append(r)
                continue  # inserted by import_jobs once the ledger is loaded
            active = 0 if old == "closed" else 1
            cur = con.execute(
                "INSERT OR IGNORE INTO listings(id,company,title,url,category,locations,terms,degrees,sponsorship,"
                "source,date_posted,date_updated,first_seen,updated_at,ats,variant,triage_status,triage_reason,active)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["id"], r["company"], r["title"], r["url"], r["category"], r["locations"], r["terms"],
                 r["degrees"], r["sponsorship"], r["source"], r["date_posted"], r["date_updated"],
                 r["first_seen"], r["updated_at"], ats, r["variant"], tstatus, reason, active))
            inserted += cur.rowcount
    summary["listings"] = inserted
    return candidates


def import_ledger(h: sqlite3.Connection, summary: dict) -> None:
    apps = 0
    keys = 0
    with db.tx() as con:
        # Listings Hermes submitted: these carry the listing id.
        for r in h.execute("SELECT * FROM listings WHERE status IN (?,?)", SUBMITTED_STATUSES):
            before = con.execute("SELECT count(*) FROM applications").fetchone()[0]
            ledger.record_application(r["id"], r["company"] or "", r["title"] or "", r["url"] or "",
                                      origin="hermes:submitted", submitted_at=r["finished_at"] or r["updated_at"])
            apps += con.execute("SELECT count(*) FROM applications").fetchone()[0] - before
        # Every applied key (tracker/manual/agent). The Hermes key itself is kept verbatim too.
        for r in h.execute("SELECT * FROM applied ORDER BY added_at, key"):
            before = con.execute("SELECT count(*) FROM applications").fetchone()[0]
            app_id = ledger.record_application(None, r["company"] or "", r["title"] or "", r["url"] or "",
                                               origin=f"hermes:{r['origin']}", submitted_at=r["added_at"])
            apps += con.execute("SELECT count(*) FROM applications").fetchone()[0] - before
            cur = con.execute(
                "INSERT OR IGNORE INTO applied_keys(key,application_id,company,title,url,origin,added_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (r["key"], app_id, r["company"], r["title"], r["url"], f"hermes:{r['origin']}", r["added_at"]))
            keys += cur.rowcount
        present = sum(1 for r in h.execute("SELECT key FROM applied")
                      if con.execute("SELECT 1 FROM applied_keys WHERE key=?", (r["key"],)).fetchone())
    summary["applications"] = apps
    summary["applied_keys"] = present          # Hermes keys now present in goat's ledger
    summary["applied_keys_added_verbatim"] = keys  # those the canonical keys alone did not cover


def import_jobs(candidates: list[sqlite3.Row], summary: dict) -> None:
    """Hermes queued/hold/in_progress/shadow_done -> goat QUEUED jobs, newest posting first."""
    def sort_key(r):
        # newest posting first; Hermes tier A ahead of B on ties; rank 0 -> priority 0
        return (-(r["date_posted"] or 0), 0 if (r["tier"] or "B") == "A" else 1)

    ordered = sorted(candidates, key=sort_key)
    created = manual = skipped = 0
    with db.tx() as con:
        for rank, r in enumerate(ordered):
            ats = r["ats"] or triage.detect_ats(r["url"] or "")
            lane = triage.lane_for(ats)
            company, title, url = r["company"] or "", r["title"] or "", r["url"] or ""
            if lane is None:
                tstatus, reason = "manual", f"portal:{ats}"
                manual += 1
            elif ledger.already_applied(company, title, url):
                tstatus, reason = "skipped", "already_applied"
                skipped += 1
            else:
                tstatus, reason = "queued", ""
            cur = con.execute(
                "INSERT OR IGNORE INTO listings(id,company,title,url,category,locations,terms,degrees,sponsorship,"
                "source,date_posted,date_updated,first_seen,updated_at,ats,variant,triage_status,triage_reason,active)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["id"], company, title, url, r["category"], r["locations"], r["terms"], r["degrees"],
                 r["sponsorship"], r["source"], r["date_posted"], r["date_updated"], r["first_seen"],
                 r["updated_at"], ats, r["variant"], tstatus, reason, 1))
            summary["listings"] = summary.get("listings", 0) + cur.rowcount
            if tstatus != "queued":
                continue
            exists = con.execute("SELECT 1 FROM jobs WHERE listing_id=?", (r["id"],)).fetchone()
            if exists:
                continue
            ts = db.now()
            cur = con.execute(
                "INSERT INTO jobs(listing_id,status,lane,priority,attempts,apply_url,resume_variant,created_at,updated_at)"
                " VALUES(?,?,?,?,0,?,?,?,?)",
                (r["id"], states.QUEUED, lane, -rank, url, r["variant"] or "swe", r["first_seen"] or ts, ts))
            con.execute("INSERT INTO events(job_id,ts,kind,detail) VALUES(?,?,?,?)",
                        (cur.lastrowid, ts, "job:imported",
                         json.dumps({"listing_id": r["id"], "hermes_status": r["status"],
                                     "hermes_reason": (r["reason"] or "")[:300], "tier": r["tier"]})))
            created += 1
    summary["jobs_created"] = created
    summary["jobs_manual_no_lane"] = manual
    summary["jobs_skipped_already_applied"] = skipped


def import_answers(h: sqlite3.Connection, summary: dict) -> None:
    saved = aliases = skipped_secret = skipped_inconsistent = 0
    by_q: dict[str, dict] = {}
    rows = h.execute("SELECT * FROM answers WHERE source IN (?,?,?) ORDER BY n", ANSWER_SOURCES).fetchall()
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[answers.norm(r["question"])].append(r)
    for nq, rs in groups.items():
        if not nq:
            continue
        distinct = {(r["answer"] or "").strip() for r in rs}
        if len(distinct) != 1:
            skipped_inconsistent += 1
            continue
        r = rs[-1]
        if looks_secret(r["question"], r["answer"]):
            skipped_secret += 1
            continue
        by_q[nq] = {"question": r["question"], "answer": (r["answer"] or "").strip(),
                    "ref": str(r["listing_id"]), "approved": True, "key": hermes_key(r["question"])}
    gen = h.execute("SELECT * FROM answers WHERE source='generated' ORDER BY n").fetchall()
    for r in gen:  # latest draft per question wins
        nq = answers.norm(r["question"])
        if not nq or looks_secret(r["question"], r["answer"]):
            skipped_secret += 1 if nq else 0
            continue
        by_q["generated:" + nq] = {"question": r["question"], "answer": (r["answer"] or "").strip(),
                                   "ref": str(r["listing_id"]), "approved": False,
                                   "key": hermes_key(r["question"], "hermes.generated")}
    used_keys: dict[str, str] = {}
    with db.tx() as con:
        for nq, item in by_q.items():
            key = item["key"]
            if used_keys.get(key, nq) != nq:  # truncation collision -> deterministic disambiguation
                key = f"{key}_{hashlib.sha1(nq.encode()).hexdigest()[:6]}"
            used_keys[key] = nq
            live = _live_answer_row(con, key)
            if live is None or (live["answer"] or "") != item["answer"] or live["source"] != "hermes":
                answers.save_answer(key, item["answer"], answer_type="text", source="hermes",
                                    source_ref=item["ref"], approved=item["approved"])
                saved += 1
            if item["approved"]:
                # A seeded EXACT alias for the same text keeps winning (ties break on confidence);
                # substring-set hits are irrelevant because exact aliases are consulted first.
                seeded = con.execute(
                    "SELECT 1 FROM question_aliases WHERE kind='exact' AND alias=? AND key!=? AND approved=1 "
                    "AND source!='hermes' LIMIT 1", (nq, key)).fetchone()
                conf = 0.8 if seeded else 1.0
                before = con.execute("SELECT count(*) FROM question_aliases").fetchone()[0]
                answers.add_alias(item["question"], key, confidence=conf, source="hermes", con=con)
                aliases += con.execute("SELECT count(*) FROM question_aliases").fetchone()[0] - before
                con.execute("UPDATE question_aliases SET confidence=? WHERE alias=? AND key=? AND source='hermes' "
                            "AND confidence!=?", (conf, nq, key, conf))
        # Rows from an earlier import whose key is no longer produced (e.g. a renamed collision key)
        # are demoted so get()/lookup() stop returning them; their aliases are dropped.
        placeholders = ",".join("?" * len(used_keys)) or "''"
        orphans = con.execute(
            f"SELECT id, key FROM answers WHERE source='hermes' AND superseded_by IS NULL AND approved=1 "
            f"AND key NOT IN ({placeholders})", tuple(used_keys)).fetchall()
        for o in orphans:
            con.execute("UPDATE answers SET approved=0, note=COALESCE(note,'')||'[orphan:hermes_import]' WHERE id=?",
                        (o["id"],))
            con.execute("DELETE FROM question_aliases WHERE source='hermes' AND key=?", (o["key"],))
        summary["answers_orphaned"] = len(orphans)
    summary["answers"] = saved
    summary["aliases"] = aliases
    summary["answers_skipped_inconsistent"] = skipped_inconsistent
    summary["answers_skipped_secret_or_empty"] = skipped_secret


def import_events(h: sqlite3.Connection, summary: dict) -> None:
    last_n = int(db.meta_get("hermes_import.last_event_n", 0) or 0)
    rows = h.execute("SELECT * FROM events WHERE n > ? ORDER BY n DESC LIMIT ?", (last_n, EVENTS_CAP)).fetchall()
    rows.reverse()
    with db.tx() as con:
        for r in rows:
            con.execute("INSERT INTO events(job_id,ts,kind,detail) VALUES(NULL,?,?,?)",
                        (r["ts"], f"hermes:{r['kind']}",
                         json.dumps(db.redact({"listing_id": r["listing_id"], "detail": r["detail"], "n": r["n"]}),
                                    default=str)))
        if rows:
            db.meta_set("hermes_import.last_event_n", rows[-1]["n"])
    summary["events"] = len(rows)


def top_queued(limit: int = 10) -> list[dict]:
    return [dict(r) for r in db.connect().execute(
        "SELECT j.priority, l.company, l.title, l.ats, j.lane FROM jobs j JOIN listings l ON l.id=j.listing_id "
        "WHERE j.status=? ORDER BY j.priority DESC, j.created_at DESC LIMIT ?", (states.QUEUED, limit))]


def run_import(hermes_dir: Path = DEFAULT_HERMES_DIR) -> dict:
    """Run every step against the CURRENT goat connection (callers decide which file that is)."""
    h = open_hermes(Path(hermes_dir))
    try:
        summary: dict = {"hermes_dir": str(hermes_dir)}
        candidates = import_listings(h, summary)
        import_ledger(h, summary)
        import_jobs(candidates, summary)
        import_answers(h, summary)
        import_events(h, summary)
        summary["seed"] = answers.seed_from_config(SETTINGS.config_dir)
        if db.meta_get("baseline_done") is None:
            # Hermes already baselined the feed; the first goat poll must queue, not baseline.
            db.meta_set("baseline_done", {"via": "hermes_import", "ts": db.now()})
            summary["baseline_done_set"] = True
        con = db.connect()
        summary["totals"] = {
            "listings": con.execute("SELECT count(*) FROM listings").fetchone()[0],
            "jobs_queued": con.execute("SELECT count(*) FROM jobs WHERE status=?", (states.QUEUED,)).fetchone()[0],
            "applications": con.execute("SELECT count(*) FROM applications").fetchone()[0],
            "applied_keys": con.execute("SELECT count(*) FROM applied_keys").fetchone()[0],
            "answers_live": con.execute("SELECT count(*) FROM answers WHERE superseded_by IS NULL").fetchone()[0],
            "aliases": con.execute("SELECT count(*) FROM question_aliases").fetchone()[0],
            "events": con.execute("SELECT count(*) FROM events").fetchone()[0],
        }
        summary["top_queued"] = top_queued()
        return summary
    finally:
        h.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hermes-dir", type=Path, default=DEFAULT_HERMES_DIR)
    ap.add_argument("--dry-run", action="store_true", help="work on a temp copy of the goat DB")
    args = ap.parse_args(argv)

    tmpdir = None
    if args.dry_run:
        tmpdir = Path(tempfile.mkdtemp(prefix="goat-dryrun-"))
        tmp_db = tmpdir / "goat.db"
        if SETTINGS.db_path.exists():
            src = sqlite3.connect(f"file:{SETTINGS.db_path.resolve().as_posix()}?mode=ro", uri=True)
            dst = sqlite3.connect(str(tmp_db))
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
        db.reset_for_tests(tmp_db)
        print(f"[dry-run] working on a copy: {tmp_db}", file=sys.stderr)
    try:
        summary = run_import(args.hermes_dir)
        summary["dry_run"] = bool(args.dry_run)
        print(json.dumps(summary, indent=2, default=str))
        return 0
    finally:
        if tmpdir is not None:
            try:
                db._con.close()  # type: ignore[union-attr]
                db._con = None
            except Exception:  # noqa: BLE001
                pass
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
