"""Error-shaped content scan and the positive submit signal (Hermes JS, proven live)."""
from __future__ import annotations

from . import js
from .executor import Executor


async def scan_errors(ex: Executor) -> dict:
    d = await ex.eval(js.JS_PAGE_ERRORS)
    if not isinstance(d, dict):
        d = {"alerts": [], "error_text": [], "empty_required": [], "url": ex.url}
    d["clean"] = not (d.get("alerts") or d.get("error_text") or d.get("empty_required"))
    return d


def why_blocked(scan: dict) -> list[str]:
    why = []
    for a in scan.get("alerts", []):
        near = f" [{a['near']}]" if a.get("near") else ""
        why.append(f"alert({a.get('how')}){near}: {a.get('text')}")
    for e in scan.get("error_text", []):
        near = f" [{e['near']}]" if e.get("near") else ""
        why.append(f"error_text{near}: {e.get('text')}")
    for r in scan.get("empty_required", []):
        ref = f" ref={r['ref']}" if r.get("ref") else ""
        why.append(f"empty_required: {r.get('label')} ({r.get('type')}){ref}")
    return why


def format_errors(scan: dict) -> str:
    return " || ".join(why_blocked(scan))


async def submit_signal(ex: Executor) -> dict:
    d = await ex.eval(js.JS_SUBMIT_SIGNAL)
    return d if isinstance(d, dict) else {}
