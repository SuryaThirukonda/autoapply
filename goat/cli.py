"""python -m goat <command>: operator commands. The agent never runs these."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import answers, db, states
from .config import SETTINGS


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_status(_a) -> None:
    con = db.connect()
    counts = {r["status"]: r["c"] for r in con.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status")}
    lanes = {r["lane"]: r["c"] for r in con.execute("SELECT lane, COUNT(*) c FROM jobs WHERE status='queued' GROUP BY lane")}
    apps = con.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    pend = con.execute("SELECT COUNT(*) FROM pending_questions WHERE answered_at IS NULL").fetchone()[0]
    _print({"mode": SETTINGS.mode, "live_portals": SETTINGS.live_portals, "config_hash": SETTINGS.config_hash(),
            "jobs": counts, "queued_by_lane": lanes, "applications": apps, "pending_questions": pend,
            "answers": con.execute("SELECT COUNT(*) FROM answers WHERE superseded_by IS NULL").fetchone()[0],
            "cdp": SETTINGS.browser.cdp_url, "db": str(SETTINGS.db_path)})


def cmd_list(a) -> None:
    con = db.connect()
    rows = con.execute("SELECT j.id, j.status, j.lane, j.priority, j.attempts, j.hold_reason, l.company, l.title, l.ats FROM jobs j "
                       "JOIN listings l ON l.id=j.listing_id WHERE (?='' OR j.status=?) ORDER BY j.priority DESC, j.updated_at DESC LIMIT ?",
                       (a.status or "", a.status or "", a.limit)).fetchall()
    for r in rows:
        print(f"#{r['id']:<5} {r['status']:<14} {r['lane'] or '':<13} p={r['priority']:<4} a={r['attempts']} {r['ats']:<10} {r['company'][:28]:<28} {r['title'][:50]:<50} {(r['hold_reason'] or '')[:60]}")


def cmd_show(a) -> None:
    con = db.connect()
    j = con.execute("SELECT j.*, l.company, l.title, l.url, l.ats FROM jobs j JOIN listings l ON l.id=j.listing_id WHERE j.id=?", (a.job,)).fetchone()
    if not j:
        print("no such job"); return
    _print(dict(j))
    from .judge import latest
    v = latest(a.job)
    if v:
        print(f"judge: {v['verdict']} ({v['bucket']}) {v['notes'][:200]}")
    for e in con.execute("SELECT ts, kind, detail, model, tokens_in, tokens_out, cost_usd FROM events WHERE job_id=? ORDER BY n DESC LIMIT ?", (a.job, a.events)):
        print(f"{e['ts']} {e['kind']:<24} {(e['detail'] or '')[:160]} {e['model'] or ''} {e['tokens_in'] or ''}/{e['tokens_out'] or ''} ${e['cost_usd'] or 0:.4f}")


def requeue_where(prefix: str, ats: str = "", limit: int = 10, dry_run: bool = False, by: str = "operator:requeue-where") -> dict:
    """Batch requeue of held jobs whose hold_reason starts with `prefix` (optionally one ATS), newest postings
    first. Hold-only, and cloud.job_action refuses submitted/confirmed rows besides."""
    from .cloud import job_action
    con = db.connect()
    rows = con.execute("SELECT j.id, l.ats, l.company, l.title, j.attempts, j.hold_reason FROM jobs j JOIN listings l ON l.id=j.listing_id "
                       "WHERE j.status=? AND j.hold_reason LIKE ? || '%' AND (?='' OR l.ats=?) ORDER BY l.date_posted DESC, j.id LIMIT ?",
                       (states.HOLD, prefix, ats or "", ats or "", int(limit))).fetchall()
    cands = [dict(r) for r in rows]
    if dry_run or not cands:
        return {"candidates": cands, "changed": [], "refused": [], "dry_run": dry_run}
    out = job_action([c["id"] for c in cands], "requeue", by)
    return {"candidates": cands, "changed": out.get("changed", []), "refused": out.get("refused", []), "dry_run": False}


def cmd_requeue_where(a) -> None:
    out = requeue_where(a.reason_prefix, a.ats, a.limit, a.dry_run)
    for c in out["candidates"]:
        mark = "would requeue" if a.dry_run else ("requeued" if c["id"] in out["changed"] else "refused")
        print(f"#{c['id']:<5} {c['ats']:<10} {c['company'][:28]:<28} {c['title'][:44]:<44} a={c['attempts']} {mark}")
    print(f"{len(out['changed'])} requeued, {len(out['refused'])} refused, {len(out['candidates'])} candidates")


def holds_report(since_s: int = 7200) -> dict:
    """What the holds say: reasons by prefix, the error holds by wording, per-ATS counts, recent outcomes."""
    con = db.connect()
    now = db.now()
    by_prefix = [dict(r) for r in con.execute(
        "SELECT substr(hold_reason, 1, instr(hold_reason || ':', ':') - 1) AS reason, COUNT(*) AS n FROM jobs WHERE status=? "
        "GROUP BY reason ORDER BY n DESC", (states.HOLD,))]
    errors = [dict(r) for r in con.execute(
        "SELECT substr(hold_reason, 1, 60) AS wording, COUNT(*) AS n FROM jobs WHERE status=? AND hold_reason LIKE 'error:%' "
        "GROUP BY wording ORDER BY n DESC LIMIT 10", (states.HOLD,))]
    top = by_prefix[0]["reason"] if by_prefix else ""
    per_ats = [dict(r) for r in con.execute(
        "SELECT l.ats, COUNT(*) AS n FROM jobs j JOIN listings l ON l.id=j.listing_id WHERE j.status=? AND j.hold_reason LIKE ? || '%' "
        "GROUP BY l.ats ORDER BY n DESC", (states.HOLD, top))] if top else []
    recent = [dict(r) for r in con.execute(
        "SELECT j.id, j.status, l.ats, l.company, l.title, substr(j.hold_reason, 1, 80) AS reason FROM jobs j JOIN listings l ON l.id=j.listing_id "
        "WHERE j.updated_at > ? AND j.status NOT IN ('new','queued') ORDER BY j.updated_at DESC LIMIT 60", (now - since_s,))]
    apps = con.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    return {"holds_by_reason": by_prefix, "error_holds": errors, "top_reason": top, "top_reason_by_ats": per_ats, "recent": recent, "applications": apps}


def cmd_holds_report(a) -> None:
    rep = holds_report(int(a.since_hours * 3600))
    if a.json:
        _print(rep); return
    print("holds by reason:")
    for r in rep["holds_by_reason"]:
        print(f"  {r['n']:>4}  {r['reason']}")
    if rep["error_holds"]:
        print("error holds by wording:")
        for r in rep["error_holds"]:
            print(f"  {r['n']:>4}  {r['wording']}")
    if rep["top_reason_by_ats"]:
        print(f"'{rep['top_reason']}' by ATS: " + ", ".join(f"{r['ats']} {r['n']}" for r in rep["top_reason_by_ats"]))
    print(f"recent outcomes (last {a.since_hours:g} h):")
    for r in rep["recent"]:
        print(f"  #{r['id']:<5} {r['status']:<14} {r['ats']:<10} {r['company'][:26]:<26} {r['title'][:40]:<40} {r['reason'] or ''}")
    print(f"applications in the ledger: {rep['applications']}")


def cmd_proposals(a) -> None:
    from . import nightly
    if a.run_nightly:
        _print(nightly.run_nightly())
    if a.approve:
        _print(nightly.apply_proposal(a.approve))
    if a.reject:
        _print(nightly.reject_proposal(a.reject))
    rows = nightly.list_proposals(a.status or None)
    if not rows:
        print("no proposals")
    for r in rows:
        print(f"{r['id']:>4} {r['kind']:<6} {r['status']:<9} {r['title'][:110]}")


def cmd_requeue(a) -> None:
    with db.tx() as con:
        con.execute("UPDATE jobs SET status=?, hold_reason=NULL, blocked_on_question_key=NULL, attempts=0, failed_submits=0, steps_used=0, wall_ms_used=0, priority=priority+5, updated_at=? WHERE id=?",
                    (states.QUEUED, db.now(), a.job))
    db.event(a.job, "job:requeued", {"by": "operator"})
    print("requeued", a.job)


def cmd_hold(a) -> None:
    with db.tx() as con:
        con.execute("UPDATE jobs SET status=?, hold_reason=?, updated_at=? WHERE id=?", (states.HOLD, f"manual_review: {a.reason}", db.now(), a.job))
    db.event(a.job, "job:hold", {"reason": a.reason, "by": "operator"})
    print("held", a.job)


def cmd_dead(a) -> None:
    with db.tx() as con:
        con.execute("UPDATE jobs SET status=?, hold_reason=?, updated_at=?, finished_at=? WHERE id=?", (states.DEAD, a.reason, db.now(), db.now(), a.job))
    print("dead", a.job)


def cmd_seed(_a) -> None:
    _print(answers.seed_from_config(SETTINGS.config_dir))


def cmd_seed_questionnaire(a) -> None:
    """Seed answers from config/questionnaire.yaml. Honors `type` (text, choice_chain, blank,
    conditional:*), exact `aliases`, phrase `match` sets and `ask_for_companies` (company-scoped
    "Surya decides" rows). Items whose answer is null are listed, not stored."""
    _print(seed_questionnaire())


def seed_questionnaire(path=None) -> dict:
    import json as _json

    import yaml
    from . import db
    p = path or (SETTINGS.config_dir / "questionnaire.yaml")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    saved, released, skipped, unchanged = [], 0, [], 0
    for section, items in data.items():
        if not isinstance(items, dict):
            continue
        for key, item in items.items():
            if not isinstance(item, dict):
                continue
            atype = str(item.get("type") or "text")
            ans = item.get("answer")
            if atype == "blank":
                ans = ""
            elif ans is None or str(ans).strip() == "":
                skipped.append(key)
                continue
            if isinstance(ans, list):
                ans = "; ".join(str(x) for x in ans)
            note = str(item.get("question", ""))[:200]
            aliases = [str(x) for x in (item.get("aliases") or [])]
            # `key:` lets one YAML item store under another item's key; `company:` scopes it to one employer
            # (that company's row beats the global answer), so the same key can carry a per-company exception.
            key = str(item.get("key") or key)
            company = str(item.get("company") or "")
            if company:
                cur_c = answers.get(key, company)
                if cur_c is not None and cur_c.scope == "company" and cur_c.answer == str(ans) and (cur_c.answer_type or "text") == atype:
                    unchanged += 1
                else:
                    released += answers.save_answer(key, str(ans), answer_type=atype, source="questionnaire", note=note,
                                                    scope="company", scope_value=company, aliases=aliases)
                    saved.append(f"{key} @ {company}")
            else:
                cur = db.connect().execute(
                    "SELECT answer, answer_type FROM answers WHERE key=? AND scope='global' AND superseded_by IS NULL ORDER BY id DESC LIMIT 1",
                    (key,)).fetchone()
                if cur is not None and cur["answer"] == str(ans) and (cur["answer_type"] or "text") == atype:
                    for al in aliases:
                        answers.add_alias(al, key, source="questionnaire")
                    unchanged += 1
                else:
                    released += answers.save_answer(key, str(ans), answer_type=atype, source="questionnaire", note=note, aliases=aliases)
                    saved.append(key)
            # Phrase sets: each entry is a phrase or a list of phrases that must all appear in the question.
            sets = []
            for m in item.get("match") or []:
                needles = [str(x) for x in (m if isinstance(m, list) else [m]) if str(x).strip()]
                if needles:
                    sets.append(needles)
            if sets or not company:
                with db.tx() as con:   # an edited match list replaces the old one
                    con.execute("DELETE FROM question_aliases WHERE key=? AND kind='substrings' AND source='questionnaire'", (key,))
            if sets:
                answers.add_alias(_json.dumps(sets), key, kind="substrings", confidence=1.0, source="questionnaire")
            for company in item.get("ask_for_companies") or []:
                cur_c = answers.get(key, str(company))
                if cur_c is None or cur_c.scope != "company":
                    answers.save_answer(key, None, answer_type=atype, source="questionnaire", scope="company",
                                        scope_value=str(company), note=f"Surya decides this one for {company}")
    return {"saved": saved, "unchanged": unchanged, "released_jobs": released, "still_null": skipped}


def cmd_lookup(a) -> None:
    r = answers.lookup(a.question)
    _print(r.__dict__ if r else {"miss": True, "hint": "no deterministic match; the run would ask the classifier, then Surya"})


def cmd_remember(a) -> None:
    released = answers.save_answer(a.key, a.value, source="operator", aliases=[a.alias] if a.alias else [])
    print(f"saved {a.key}; released {released} held job(s)")


def cmd_import_hermes(a) -> None:
    import runpy
    sys.argv = ["import_hermes.py"] + (["--dry-run"] if a.dry_run else [])
    runpy.run_path(str(SETTINGS.config_dir.parent / "scripts" / "import_hermes.py"), run_name="__main__")


def cmd_run_once(a) -> None:
    """Run exactly one job (the next queued, or --job N) and exit. --shadow forces the gate to stop before submit."""
    from . import worker
    from .supervisor import setup_logging
    setup_logging()
    if a.shadow:
        SETTINGS.mode = "shadow"
    if a.job:
        with db.tx() as con:
            con.execute("UPDATE jobs SET status=?, priority=priority+1000, updated_at=? WHERE id=?", (states.QUEUED, db.now(), a.job))
    job = worker.claim_next()
    if not job:
        print("nothing queued"); return
    out = asyncio.run(worker.run_job(job, shadow_override=a.shadow))
    _print({"job": job["id"], "company": job["company"], "title": job["title"], "outcome": out.status, "reason": out.reason,
            "records": str(SETTINGS.records_dir / str(job["id"]))})


def cmd_run(a) -> None:
    from . import supervisor
    asyncio.run(supervisor.main(run_worker=not a.no_worker, run_scraper=not a.no_scraper))


def cmd_scrape(_a) -> None:
    from . import scraper
    _print(scraper.poll_once())


def cmd_gmail_auth(_a) -> None:
    from .mail import gmail
    print("token written to", gmail.authorize_interactive())


def cmd_digest(_a) -> None:
    from .supervisor import digest_text
    print(digest_text())


def cmd_check_api(a) -> None:
    import runpy
    sys.argv = ["phase0_api_checks.py"] + (["--models", a.models] if a.models else [])
    runpy.run_path(str(SETTINGS.config_dir.parent / "scripts" / "phase0_api_checks.py"), run_name="__main__")


def cmd_dashboard(a) -> None:
    from .dashboard import serve
    from .supervisor import setup_logging
    setup_logging()
    print(f"dashboard: http://127.0.0.1:{a.port}")
    serve(a.port).serve_forever()


def cmd_demo(a) -> None:
    import runpy
    sys.argv = ["demo_workday.py"] + (["--live"] if a.live else []) + (["--real"] if a.real else [])
    runpy.run_path(str(SETTINGS.config_dir.parent / "scripts" / "demo_workday.py"), run_name="__main__")


def cmd_cloud_sync(_a) -> None:
    """Mirror everything to RDS once (no worker, no browser): fills the website after a deploy."""
    from .cloud import CloudLink
    if not SETTINGS.cloud.enabled:
        print("GOAT_DB_URL is not set in .env: the cloud link is off"); return
    link = CloudLink()
    # No heartbeat: the agent is not running, and a heartbeat that stops would read as "went silent".
    link._last_heartbeat = float("inf")
    total: dict = {}
    try:
        while True:
            sent = link.push_now()
            for k, v in sent.items():
                total[k] = total.get(k, 0) + v
            if sent.get("events", 0) < 500 and sent.get("screenshots", 0) < 8 and not link._shot_backlog:
                break
    finally:
        link._close_push()
    _print({"sent": total})


def cmd_chrome(_a) -> None:
    from .browser import chrome
    ok = asyncio.run(chrome.ensure_running())
    _print({"cdp": SETTINGS.browser.cdp_url, "healthy": ok, "profile": SETTINGS.browser.user_data_dir})


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="goat")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    s = sub.add_parser("list"); s.add_argument("--status", default=""); s.add_argument("--limit", type=int, default=40); s.set_defaults(fn=cmd_list)
    s = sub.add_parser("show"); s.add_argument("job", type=int); s.add_argument("--events", type=int, default=30); s.set_defaults(fn=cmd_show)
    s = sub.add_parser("requeue"); s.add_argument("job", type=int); s.set_defaults(fn=cmd_requeue)
    s = sub.add_parser("hold"); s.add_argument("job", type=int); s.add_argument("--reason", required=True); s.set_defaults(fn=cmd_hold)
    s = sub.add_parser("dead"); s.add_argument("job", type=int); s.add_argument("--reason", default="operator"); s.set_defaults(fn=cmd_dead)
    sub.add_parser("seed").set_defaults(fn=cmd_seed)
    sub.add_parser("seed-questionnaire").set_defaults(fn=cmd_seed_questionnaire)
    s = sub.add_parser("lookup"); s.add_argument("question"); s.set_defaults(fn=cmd_lookup)
    s = sub.add_parser("remember"); s.add_argument("key"); s.add_argument("value"); s.add_argument("--alias", default=""); s.set_defaults(fn=cmd_remember)
    s = sub.add_parser("import-hermes"); s.add_argument("--dry-run", action="store_true"); s.set_defaults(fn=cmd_import_hermes)
    s = sub.add_parser("run-once"); s.add_argument("--job", type=int, default=0); s.add_argument("--shadow", action="store_true"); s.set_defaults(fn=cmd_run_once)
    s = sub.add_parser("run"); s.add_argument("--no-worker", action="store_true"); s.add_argument("--no-scraper", action="store_true"); s.set_defaults(fn=cmd_run)
    sub.add_parser("scrape").set_defaults(fn=cmd_scrape)
    sub.add_parser("gmail-auth").set_defaults(fn=cmd_gmail_auth)
    sub.add_parser("digest").set_defaults(fn=cmd_digest)
    s = sub.add_parser("check-api"); s.add_argument("--models", default=""); s.set_defaults(fn=cmd_check_api)
    sub.add_parser("chrome").set_defaults(fn=cmd_chrome)
    sub.add_parser("cloud-sync").set_defaults(fn=cmd_cloud_sync)
    s = sub.add_parser("dashboard"); s.add_argument("--port", type=int, default=8787); s.set_defaults(fn=cmd_dashboard)
    s = sub.add_parser("demo"); s.add_argument("--live", action="store_true"); s.add_argument("--real", action="store_true"); s.set_defaults(fn=cmd_demo)
    s = sub.add_parser("requeue-where"); s.add_argument("--reason-prefix", required=True); s.add_argument("--ats", default="")
    s.add_argument("--limit", type=int, default=10); s.add_argument("--dry-run", action="store_true"); s.set_defaults(fn=cmd_requeue_where)
    s = sub.add_parser("holds-report"); s.add_argument("--since-hours", type=float, default=2.0); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_holds_report)
    s = sub.add_parser("proposals"); s.add_argument("--approve", type=int, default=0); s.add_argument("--reject", type=int, default=0)
    s.add_argument("--run-nightly", action="store_true"); s.add_argument("--status", default=""); s.set_defaults(fn=cmd_proposals)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
