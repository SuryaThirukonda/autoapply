"""Write every queued, held and ready-for-you job with its posting date to a Markdown file (default
data/queue_by_post_date.md), so the queue can be read and re-sorted by posting date.

    .venv\\Scripts\\python.exe scripts\\queue_report.py [--out PATH]

The queued table is in claim order (what the worker takes next); the other tables are newest posting first.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from goat import db, triage, worker  # noqa: E402

IN_FLIGHT = ("filling", "gated", "submitting", "awaiting_answer", "awaiting_code", "claimed")


def _day(ts) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(int(ts))) if ts else "unknown"


def _cell(s, n: int = 90) -> str:
    s = " ".join(str(s or "").split()).replace("|", "/")
    return s if len(s) <= n else s[: n - 1] + "…"


def _fit(title: str) -> str:
    try:
        return triage.role_fit(title)
    except Exception:  # noqa: BLE001
        return "?"


def rows(con, statuses: tuple[str, ...]) -> list[dict]:
    marks = ",".join("?" * len(statuses))
    out = []
    for r in con.execute(
            "SELECT j.id, j.status, j.priority, j.attempts, j.hold_reason, j.blocked_on_question_key, l.company, l.title, l.ats, "
            "l.date_posted, l.first_seen, l.url FROM jobs j JOIN listings l ON l.id=j.listing_id "
            f"WHERE j.status IN ({marks})", statuses):
        d = dict(r)
        d["posted"] = d["date_posted"] or d["first_seen"] or 0
        d["posted_label"] = _day(d["posted"]) + ("" if d["date_posted"] else " (first seen)")
        d["fit"] = _fit(d["title"])
        out.append(d)
    return out


def build(now: int | None = None) -> str:
    now = int(now or time.time())
    con = db.connect()
    with db.tx() as tx:
        worker.rank_queue(tx, now)
    queued = sorted(rows(con, ("queued",)), key=lambda d: (-int(d["priority"] or 0), -int(d["posted"] or 0)))
    flight = rows(con, IN_FLIGHT)
    held = sorted(rows(con, ("hold",)), key=lambda d: -int(d["posted"] or 0))
    rfy = sorted(rows(con, ("ready_for_you",)), key=lambda d: -int(d["posted"] or 0))

    def age(d) -> str:
        return f"{max(0, (now - int(d['posted'])) // 86400)}d" if d["posted"] else ""

    lines = [f"# Queue and holds by posting date",
             "",
             f"Generated {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}. "
             f"{len(queued)} queued, {len(flight)} in flight, {len(held)} on hold, {len(rfy)} ready for you.",
             "",
             "Role fit: **aligned** = SWE, SDE, application development, programming, technology, data science / engineering / "
             "analytics. The newest posting is claimed first; within one posting day an aligned title goes first. Priority "
             "1000 and above is an explicit choice (release, pin, drag order, answered question) and goes ahead of all of that.",
             ""]
    if flight:
        lines += ["## In flight", "", "| Job | Company | Title | Portal | Posted | Status |", "|---|---|---|---|---|---|"]
        for d in flight:
            lines.append(f"| {d['id']} | {_cell(d['company'], 40)} | {_cell(d['title'], 60)} | {d['ats']} | {d['posted_label']} | {d['status']} |")
        lines.append("")
    lines += ["## Queued, in claim order", "",
              "| # | Job | Company | Title | Portal | Posted | Age | Role fit | Priority |",
              "|---|---|---|---|---|---|---|---|---|"]
    for i, d in enumerate(queued, 1):
        lines.append(f"| {i} | {d['id']} | {_cell(d['company'], 40)} | {_cell(d['title'], 60)} | {d['ats']} | {d['posted_label']} | "
                     f"{age(d)} | {d['fit']} | {d['priority']} |")
    for name, items in (("On hold", held), ("Ready for you", rfy)):
        lines += ["", f"## {name}, newest posting first", "",
                  "| Job | Company | Title | Portal | Posted | Age | Role fit | Reason |",
                  "|---|---|---|---|---|---|---|---|"]
        for d in items:
            reason = d["hold_reason"] or ""
            if d["blocked_on_question_key"]:
                reason = f"waiting on {d['blocked_on_question_key']}: {reason}"
            lines.append(f"| {d['id']} | {_cell(d['company'], 40)} | {_cell(d['title'], 60)} | {d['ats']} | {d['posted_label']} | "
                         f"{age(d)} | {d['fit']} | {_cell(reason, 110)} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data" / "queue_by_post_date.md"))
    a = ap.parse_args()
    Path(a.out).write_text(build(), encoding="utf-8")
    print("wrote", a.out)


if __name__ == "__main__":
    main()
