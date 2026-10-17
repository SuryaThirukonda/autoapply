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


def record_decision(job_id: int, page: int, field_label: str, value: Any, source: str, ref: str = "") -> None:
    """Every field the agent set, with its source. `source` is one of:
    standing:<key> | bank:<id> | adapter:<name> | approved_wording:<key> | policy:<key> |
    resume | user:<question key> | judgment (judgment must be explained in notes)."""
    v = "(set)" if any(s in field_label.lower() for s in ("password", "verification code", "security code")) else value
    with db.tx() as con:
        con.execute("INSERT INTO decisions(job_id,page,field_label,value,source,ref,ts) VALUES(?,?,?,?,?,?,?)",
                    (job_id, page, field_label, None if v is None else str(v)[:2000], source, ref, db.now()))


def decisions(job_id: int) -> list[dict]:
    return [dict(r) for r in db.connect().execute(
        "SELECT page, field_label, value, source, ref, ts FROM decisions WHERE job_id=? ORDER BY id", (job_id,))]


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
