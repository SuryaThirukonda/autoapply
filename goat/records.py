"""Per-job records: records/<job_id>/ holds the decisions record, a screenshot per action, and
the full model request/response on failure. Everything written here is redacted first."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from . import db
from .config import SETTINGS

_SECRET_SHAPES = [
    re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*\S+"),
    re.compile(r"\b[A-Za-z0-9]{6,10}\b(?=\s*(?:is your|verification|security) code)"),
]


def job_dir(job_id: int) -> Path:
    d = SETTINGS.records_dir / str(job_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def scrub_text(text: str) -> str:
    for pat in _SECRET_SHAPES:
        text = pat.sub("[redacted]", text)
    return text


def save_screenshot(job_id: int, png: bytes, tag: str) -> Path:
    p = job_dir(job_id) / f"{int(time.time() * 1000)}_{re.sub(r'[^a-z0-9_-]+', '_', tag.lower())[:40]}.png"
    p.write_bytes(png)
    return p


def save_json(job_id: int, name: str, obj: Any) -> Path:
    p = job_dir(job_id) / name
    p.write_text(json.dumps(db.redact(obj), indent=2, default=str), encoding="utf-8")
    return p


def record_decision(job_id: int, page: int, field_label: str, value: Any, source: str, ref: str = "", tier: str | None = None) -> None:
    """Every field the agent set, with its source. `source` is one of:
    standing:<key> | bank:<id> | adapter:<name> | approved_wording:<key> | policy:<key> |
    resume | user:<question key> | judgment (judgment must be explained in notes).
    `tier` (confirmed | inferred | sensitive) is derived from the source when not given."""
    from . import answers
    v = "(set)" if any(s in field_label.lower() for s in ("password", "verification code", "security code")) else value
    tier = tier or answers.decision_tier(source, field_label)
    with db.tx() as con:
        con.execute("INSERT INTO decisions(job_id,page,field_label,value,source,ref,ts,tier) VALUES(?,?,?,?,?,?,?,?)",
                    (job_id, page, field_label, None if v is None else str(v)[:2000], source, ref, db.now(), tier))


def decisions(job_id: int) -> list[dict]:
    return [dict(r) for r in db.connect().execute(
        "SELECT page, field_label, value, source, ref, ts, tier FROM decisions WHERE job_id=? ORDER BY id", (job_id,))]


def inferred_decisions(job_id: int, limit: int = 12) -> list[dict]:
    """The decisions Surya did not confirm himself (newest per label), numbered for a one-tap review:
    'all good' or 'fix 2: <value>' in the finish message."""
    latest: dict[str, dict] = {}
    for d in decisions(job_id):
        if d.get("tier") != "inferred" or str(d.get("value") or "") in ("", "(set)"):
            continue
        src = str(d.get("source") or "")
        key = src.split(":", 1)[1].split(" ", 1)[0] if ":" in src and src.lower().startswith(("standing:", "bank:")) else ""
        latest[d["field_label"]] = {"label": str(d["field_label"])[:80], "value": str(d["value"])[:120], "source": src[:60], "key": key}
    out = list(latest.values())[:limit]
    for i, item in enumerate(out, 1):
        item["n"] = i
    return out


def archived_rows(job_id: int) -> list[dict]:
    """Every decision the worker archived from earlier attempts (decisions_attemptN.md), oldest attempt first;
    each row carries its attempt number."""
    try:
        files = sorted(job_dir(job_id).glob("decisions_attempt*.md"), key=lambda p: int(p.stem.rsplit("attempt", 1)[1]))
    except Exception:
        return []
    out: list[dict] = []
    for f in files:
        attempt = int(f.stem.rsplit("attempt", 1)[1])
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.startswith("| ") or line.startswith(("| Page", "|---")):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 5:
                continue
            out.append({"page": cells[0], "field_label": cells[1], "value": "|".join(cells[2:-2]).strip(),
                        "source": cells[-2], "archived": True, "attempt": attempt})
    return out


def _question_part(label: str) -> str:
    """'Previously employed? / No' -> 'previously employed?': radio and checkbox decisions name question / option."""
    return str(label or "").split(" / ", 1)[0].strip().lower()


def answer_set(job_id: int) -> list[dict]:
    """The application's answers as one list: archived attempts first (a resumed draft keeps what an earlier
    attempt typed), then this attempt's decisions. A later attempt replaces every earlier entry for a question it
    answered (so an old 'Q / Yes' does not survive a newer 'Q / No'); within one attempt the newest per label wins."""
    rows = archived_rows(job_id) + [{**d, "archived": False, "attempt": 10 ** 6} for d in decisions(job_id)]
    by_attempt: dict[int, list[dict]] = {}
    for r in rows:
        by_attempt.setdefault(int(r.get("attempt") or 0), []).append(r)
    latest: dict[str, dict] = {}
    for att in sorted(by_attempt):
        batch = [r for r in by_attempt[att] if str(r.get("field_label") or "").strip()]
        questions = {_question_part(r["field_label"]) for r in batch}
        for label in [k for k in latest if _question_part(k) in questions]:
            del latest[label]
        for r in batch:
            label = str(r["field_label"]).strip()
            latest.pop(label, None)
            latest[label] = r
    return list(latest.values())


def unconfirmed_answers(job_id: int, company: str = "", ats: str = "", limit: int = 30) -> list[dict]:
    """The answers on this application that Surya never confirmed (see answers.unconfirmed_source), with the
    evidence the agent had, for the fact check before an automatic submit."""
    from . import answers
    out = []
    for d in answer_set(job_id):
        val = str(d.get("value") or "")
        if val in ("", "(set)"):
            continue
        unconfirmed, evidence = answers.unconfirmed_source(d.get("source"), company, ats)
        if not unconfirmed:
            continue
        out.append({"label": str(d.get("field_label") or "")[:120], "value": val[:1500], "source": str(d.get("source") or "")[:80],
                    "evidence": evidence, "archived": bool(d.get("archived"))})
    return out[:limit]


def inferred_text(items: list[dict], max_chars: int = 300) -> str:
    """The tail of a finish message: the inferred decisions and how to confirm or fix them."""
    if not items:
        return ""
    head = f"{len(items)} inferred: " + "; ".join(f"{it['n']}) {it['label'][:30]} = {it['value'][:30]}" for it in items)
    tail = " Reply 'all good' or 'fix 2: <value>'."
    return (head[: max_chars - len(tail) - 1].rstrip() + ("…" if len(head) > max_chars - len(tail) - 1 else "")) + tail


def write_decisions_markdown(job_id: int, header: dict, free_text: list[dict], notes: list[str]) -> Path:
    rows = decisions(job_id)
    lines = [f"# Decisions: {header.get('company', '')} - {header.get('title', '')}", ""]
    for k, v in header.items():
        lines.append(f"- {k}: {v}")
    lines += ["", "## Every field on the form", "", "| Page | Field label | Value on the page | Source |", "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['page']} | {r['field_label']} | {str(r['value'] or '').replace('|', '/')[:200]} | {r['source']} |")
    lines += ["", "## Free-response answers", ""]
    if not free_text:
        lines.append("None on this form.")
    for ft in free_text:
        lines += [f"### {ft['question']}", "", scrub_text(ft["answer"]), "", f"Facts used: {ft.get('facts', '')}", ""]
    lines += ["", "## Things Surya should know", ""]
    lines += [f"- {n}" for n in notes] or ["- None."]
    p = job_dir(job_id) / "decisions.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def decisions_without_source(job_id: int) -> list[dict]:
    return [r for r in decisions(job_id) if not r["source"] or r["source"] == "judgment"]
