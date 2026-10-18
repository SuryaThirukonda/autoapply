"""The submit gate: deterministic checks that return named results. It passes only when the page
scans clean, every required field is filled, every decision has a source, attestations come only
from standing answers, the résumé on the page is the expected file, the listing is not in the
ledger, no challenge is present, and the page carries no prohibition on automated applications."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache

from . import ledger, records
from .browser.errors import why_blocked
from .browser.snapshot import Snapshot
from .config import SETTINGS

ATTESTATION = re.compile(r"(certif|attest|i confirm|acknowledge|true and (complete|accurate|correct)|truthful|under penalty|electronic signature|signature)", re.I)
ALLOWED_ATTESTATION_SOURCES = ("standing:", "policy:", "bank:", "seed:", "standing:consent.")


@dataclass
class GateResult:
    result: str                       # pass_live | pass_shadow | blocked | captcha_present | automation_prohibited | guardrail_block | already_applied
    reasons: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.result in ("pass_live", "pass_shadow")

    def as_dict(self) -> dict:
        return {"result": self.result, "reasons": self.reasons}


@lru_cache(maxsize=1)
def _patterns() -> tuple[dict, dict]:
    trap = json.loads((SETTINGS.config_dir / "trap_patterns.json").read_text(encoding="utf-8"))
    guard = json.loads((SETTINGS.config_dir / "guardrail_patterns.json").read_text(encoding="utf-8"))
    return trap, guard


def scan_page_prohibitions(page_text: str) -> list[str]:
    trap, _ = _patterns()
    hits = []
    for name, pat in (trap.get("hard") or {}).items():
        m = re.search(pat, page_text or "", re.I)
        if m:
            hits.append(f"{name}: {page_text[max(0, m.start() - 40): m.end() + 40].strip()}")
    return hits


# The portal itself failed (not the form): Workday's "Something went wrong ... Error Code: VPS|...",
# server errors, maintenance pages. Nothing on the form can fix these; they are retried later.
PORTAL_ERROR = re.compile(
    r"(something went wrong.{0,160}(error code|refresh the page|try again)|error code:\s*vps\b|"
    r"\b(500|502|503|504)\b.{0,40}(internal server error|bad gateway|service unavailable|gateway time-?out)|"
    r"(we are|we're|we['\u2019]re) (currently )?(experiencing technical difficulties|performing (scheduled )?maintenance)|"
    r"(site|system|service) is (temporarily )?(unavailable|down for maintenance))", re.I | re.S)


def portal_error(page_text: str) -> str | None:
    """A short quote of the portal error on the page, or None."""
    m = PORTAL_ERROR.search(page_text or "")
    if not m:
        return None
    return " ".join((page_text or "")[max(0, m.start() - 20): m.end() + 60].split())[:200]


def guardrail_check(text: str) -> tuple[list[str], list[str]]:
    """(blocks, warns) for a generated answer."""
    _, guard = _patterns()
    blocks = [n for n, p in (guard.get("block") or {}).items() if re.search(p, text or "", re.I)]
    warns = [n for n, p in (guard.get("warn") or {}).items() if re.search(p, text or "", re.I)]
    return blocks, warns


def run_gate(*, job: dict, snapshot: Snapshot, page_text: str, expected_resume: str | None,
             free_text_answers: list[str], portal: str) -> GateResult:
    reasons: list[str] = []

    # 0. ledger
    if ledger.already_applied(job.get("company", ""), job.get("title", ""), job.get("url", "")) and not job.get("_ledger_ok"):
        return GateResult("already_applied", ["listing is already in the ledger"])

    # 1. page prohibitions / traps
    hits = scan_page_prohibitions(page_text)
    if hits:
        return GateResult("automation_prohibited", hits)

    # 1b. a sign-in or account page is never the end of an application
    if any(i.get("type") == "password" for i in snapshot.items):
        return GateResult("blocked", ["this is a sign-in or account page (password field), not the application form: sign in, then fill the form"])

    # 2. challenge
    if snapshot.challenge.get("challenge"):
        return GateResult("captcha_present", [f"human-verification challenge: {', '.join(snapshot.challenge.get('kinds') or [])}"])

    # 3. page errors and required fields
    # Workday's Review page lists every section; "No Response" under Work Experience (he has four roles) or
    # under Education's Field of Study means a section was skipped: go back and fill it (2026-10-18).
    if (portal or "").lower() == "workday" and re.search(r"\bReview\b", page_text[:3000]):
        gaps = [sec for sec in ("Work Experience", "Field of Study") if re.search(sec + r"\s*\n\s*No Response", page_text)]
        if gaps:
            return GateResult("blocked", [f"the Review page shows '{g}: No Response': call go_to_step('My Experience'), fill it from the experience.* keys, "
                                          "then Save and Continue forward to Review" for g in gaps])
    why = why_blocked(snapshot.errors)
    reasons += why

    # 4. decisions: every value has a source; attestations only from standing answers
    decs = records.decisions(job["id"])
    for d in decs:
        if not d["source"] or d["source"] == "judgment":
            reasons.append(f"decision without an approved source: {d['field_label']}")
        if ATTESTATION.search(d["field_label"] or "") and not str(d["source"]).startswith(ALLOWED_ATTESTATION_SOURCES):
            reasons.append(f"attestation not from a standing answer: {d['field_label']} (source {d['source']})")
        if d["source"] == "generated" and str(d["value"] or "").strip().lower() in ("checked", "unchecked", "true", "false", "yes", "no"):
            reasons.append(f"choice set from generated text, not a stored answer: {d['field_label']}")

    # 5. résumé on the page: whenever the form has a résumé slot, the expected file must be in it.
    resume_slots = [fi for fi in snapshot.file_inputs()
                    if re.search(r"resume|résumé|\bcv\b|curriculum", " ".join([fi.get("label") or "", fi.get("name") or ""]), re.I)
                    and not re.search(r"cover|transcript", " ".join([fi.get("label") or "", fi.get("name") or ""]), re.I)]
    if resume_slots:
        shown = " ".join((fi.get("value") or "") for fi in resume_slots).lower()
        if not shown:
            reasons.append("resume not attached: the Resume/CV slot is empty (call upload_resume on its ref)")
        elif expected_resume and expected_resume.lower() not in shown:
            reasons.append(f"resume slot holds {shown!r}, expected {expected_resume}")
    elif expected_resume and snapshot.file_inputs():
        shown = " ".join((fi.get("value") or "") for fi in snapshot.file_inputs()).lower()
        if expected_resume.lower() not in shown and expected_resume.lower() not in (page_text or "").lower():
            reasons.append(f"resume not shown on page: expected {expected_resume}")

    # 5b. every value on the page has a sourced decision from THIS attempt (by ref, else by label words).
    def _nw(s: str) -> set:
        return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 2}
    dec_refs = {d["ref"] for d in decs if d.get("ref")}
    dec_labels = [_nw(d["field_label"]) for d in decs]
    def _sourced(ref: str, label: str) -> bool:
        if ref in dec_refs:
            return True
        lw = _nw(label)
        return any(dl and (dl <= lw or (lw and lw <= dl)) for dl in dec_labels)
    for it in snapshot.items:
        if it.get("tag") not in ("input", "textarea", "select", "dropdown", "richtext", "toggle") or it.get("type") in ("file", "password", "hidden", "submit", "button"):
            continue
        val = it.get("shown") or it.get("value") or ""
        if it.get("type") == "checkbox":
            val = "checked" if it.get("checked") else ""
        if it.get("tag") == "toggle":
            val = "pressed" if it.get("pressed") else ""
        if not str(val).strip():
            continue
        if not _sourced(it.get("ref", ""), it.get("label", "")):
            reasons.append(f"value on page with no sourced decision this attempt: [{it.get('ref')}] {(it.get('label') or '')[:60]} = {str(val)[:40]!r} (re-set it from its answer_key with choose/fill_batch, or clear it)")
    for g in snapshot.groups:
        for o in g.get("options", []):
            if o.get("checked") and not _sourced(o.get("ref", ""), f"{g.get('label', '')} {o.get('label', '')}"):
                reasons.append(f"checked option with no sourced decision this attempt: [{o.get('ref')}] {(g.get('label') or '')[:50]} / {(o.get('label') or '')[:30]} (re-set it with fill_batch using the group's answer_key from lookup_answer, or clear it with value false source 'clear')")

    # 6. generated text against guardrails
    for t in free_text_answers:
        blocks, _ = guardrail_check(t)
        if blocks:
            return GateResult("guardrail_block", [f"guardrail {b}" for b in blocks])

    if reasons:
        return GateResult("blocked", reasons)
    return GateResult("pass_live" if SETTINGS.portal_is_live(portal) else "pass_shadow", [])
