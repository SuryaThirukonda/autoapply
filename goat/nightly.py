"""Nightly proposals (Phase 5): what the day's runs suggest changing, written as rows in `proposals` and a
Markdown report under proposals/, for Surya to approve or reject (`goat proposals`).

- alias promotion: a question mapping the classifier made (question_aliases, approved=0) that the agent
  looked up in at least two different jobs becomes an `alias` proposal; approving it marks the alias
  approved so the deterministic lookup takes it next time, without a model call.
- holds summary: the day's job:hold reasons grouped by prefix, as one `code` proposal ("holds to look at").
Nothing here changes an answer or a job by itself."""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

from . import answers, db
from .config import SETTINGS

DAY_S = 24 * 3600


def _day(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))


def alias_seen_jobs(alias_norm: str, con=None) -> set[int]:
    """Distinct jobs in which the agent looked this question up (tool:lookup_answer events)."""
    con = con or db.connect()
    out: set[int] = set()
    for r in con.execute("SELECT job_id, detail FROM events WHERE kind='tool:lookup_answer' AND job_id IS NOT NULL"):
        try:
            q = (json.loads(r["detail"] or "{}").get("args") or {}).get("question") or ""
        except Exception:
            continue
        if answers.norm(q) == alias_norm:
            out.add(int(r["job_id"]))
    return out


def _proposal_exists(con, title: str) -> bool:
    return con.execute("SELECT 1 FROM proposals WHERE title=? AND status IN ('proposed','approved','applied')", (title,)).fetchone() is not None


def promote_aliases(min_seen: int = 2, now: float | None = None) -> int:
    """Unapproved aliases seen in >= min_seen distinct jobs become `alias` proposals (one per alias+key)."""
    con = db.connect()
    rows = con.execute("SELECT alias, kind, key, confidence, source FROM question_aliases WHERE approved=0 AND kind='exact'").fetchall()
    inserted = 0
    for r in rows:
        title = f"{r['alias']} -> {r['key']}"
        jobs = alias_seen_jobs(r["alias"], con)
        if len(jobs) < min_seen or _proposal_exists(con, title):
            continue
        body = json.dumps({"alias": r["alias"], "key": r["key"], "confidence": r["confidence"], "source": r["source"]})
        evidence = json.dumps({"jobs": sorted(jobs), "seen": len(jobs)})
        with db.tx() as c:
            c.execute("INSERT INTO proposals(kind,title,body,evidence,status,created_at) VALUES('alias',?,?,?,'proposed',?)",
                      (title, body, evidence, int(now if now is not None else time.time())))
        inserted += 1
    return inserted


def summarize_holds(day_start: float, day_end: float, top: int = 12) -> dict:
    """job:hold reasons in the window grouped by their prefix (the text before the first colon)."""
    con = db.connect()
    groups: dict[str, list[int]] = defaultdict(list)
    for r in con.execute("SELECT job_id, detail FROM events WHERE kind='job:hold' AND ts>=? AND ts<? ORDER BY n", (int(day_start), int(day_end))):
        try:
            reason = str((json.loads(r["detail"] or "{}") or {}).get("reason") or "")
        except Exception:
            reason = ""
        prefix = (reason.split(":", 1)[0].strip() or "(none)")[:60]
        if r["job_id"] is not None and int(r["job_id"]) not in groups[prefix]:
            groups[prefix].append(int(r["job_id"]))
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:top]
    return {k: {"count": len(v), "jobs": v[:20]} for k, v in ordered}


def _holds_markdown(day: str, summary: dict) -> str:
    lines = [f"| reason | jobs | ids |", "|---|---|---|"]
    for k, v in summary.items():
        lines.append(f"| {k} | {v['count']} | {', '.join(str(j) for j in v['jobs'][:12])} |")
    return f"Holds on {day}\n\n" + ("\n".join(lines) if summary else "(no holds)")


def record_holds_proposal(day: str, summary: dict, now: float | None = None) -> bool:
    con = db.connect()
    title = f"holds to look at {day}"
    if _proposal_exists(con, title):
        return False
    with db.tx() as c:
        c.execute("INSERT INTO proposals(kind,title,body,evidence,status,created_at) VALUES('code',?,?,?,'proposed',?)",
                  (title, _holds_markdown(day, summary), json.dumps(summary), int(now if now is not None else time.time())))
    return True


def write_report(day: str, aliases: list[dict], holds: dict) -> Path:
    d = SETTINGS.proposals_dir
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{day}.md"
    lines = [f"# goat nightly report {day}", "", "## Alias proposals", ""]
    lines += [f"- [{a['id']}] {a['title']} (seen in {a.get('seen', '?')} jobs)" for a in aliases] or ["(none)"]
    lines += ["", "## Holds to look at", "", _holds_markdown(day, holds), ""]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def run_nightly(now: float | None = None) -> dict:
    """The whole pass: promote aliases, summarize yesterday's-and-today's holds, write the report."""
    now = time.time() if now is None else now
    day = _day(now)
    n_alias = promote_aliases(now=now)
    holds = summarize_holds(now - DAY_S, now)
    new_holds = record_holds_proposal(day, holds, now=now)
    con = db.connect()
    aliases = [dict(r) | {"seen": (json.loads(r["evidence"] or "{}") or {}).get("seen")}
               for r in con.execute("SELECT id, title, evidence FROM proposals WHERE kind='alias' AND status='proposed' ORDER BY id")]
    path = write_report(day, aliases, holds)
    counts = {"day": day, "aliases_proposed": n_alias, "holds_groups": len(holds), "holds_proposal": new_holds, "report": str(path)}
    db.event(None, "nightly:done", counts)
    return counts


def list_proposals(status: str | None = None) -> list[dict]:
    con = db.connect()
    sql = "SELECT id, kind, status, title, created_at, decided_at FROM proposals"
    args: tuple = ()
    if status:
        sql += " WHERE status=?"
        args = (status,)
    return [dict(r) for r in con.execute(sql + " ORDER BY id", args)]


def apply_proposal(proposal_id: int) -> dict:
    """Approve: an alias proposal marks its alias approved (and re-saves the answer so the store logs it);
    a code proposal is only marked approved. Rejected/applied rows are left alone."""
    con = db.connect()
    r = con.execute("SELECT id, kind, status, body FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    if not r:
        return {"ok": False, "why": "no such proposal"}
    if r["status"] in ("applied", "rejected"):
        return {"ok": False, "why": f"already {r['status']}"}
    if r["kind"] == "alias":
        body = json.loads(r["body"] or "{}")
        alias, key = body.get("alias") or "", body.get("key") or ""
        cur = answers.get(key)
        if not cur:
            return {"ok": False, "why": f"no live answer for {key}"}
        with db.tx() as c:
            c.execute("UPDATE question_aliases SET approved=1, source=COALESCE(source,'') || '+nightly:approved' WHERE alias=? AND key=?", (alias, key))
        answers.save_answer(key, cur.answer, answer_type=cur.answer_type, source="nightly:approved",
                            note=cur.note, aliases=[alias])
        new_status = "applied"
    else:
        new_status = "approved"
    with db.tx() as c:
        c.execute("UPDATE proposals SET status=?, decided_at=? WHERE id=?", (new_status, db.now(), proposal_id))
    return {"ok": True, "status": new_status}


def reject_proposal(proposal_id: int) -> dict:
    con = db.connect()
    r = con.execute("SELECT status FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    if not r:
        return {"ok": False, "why": "no such proposal"}
    if r["status"] in ("applied", "rejected"):
        return {"ok": False, "why": f"already {r['status']}"}
    with db.tx() as c:
        c.execute("UPDATE proposals SET status='rejected', decided_at=? WHERE id=?", (db.now(), proposal_id))
    return {"ok": True, "status": "rejected"}
