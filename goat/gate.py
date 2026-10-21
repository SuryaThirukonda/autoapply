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

# "certify" / "certified that" attest; a "Certification" field (a credential like AWS Cloud Practitioner) does not
# (Axiom Space's Workday Certification picker was held as an attestation, 2026-10-19)
ATTESTATION = re.compile(r"(\bcertif(y|ies|ied)\b|attest|i confirm|acknowledge|true and (complete|accurate|correct)|truthful|under penalty|electronic signature|signature)", re.I)
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
    r"(site|system|service) is (temporarily )?(unavailable|down for maintenance)|"
    # Workday's maintenance page (Northrop Grumman, 2026-10-18): "Workday is currently unavailable."
    r"\b(workday|the site|this site|the system|the portal|our careers site) is (currently |temporarily )?unavailable\b)", re.I | re.S)

# The gate passes only where an application can actually be submitted (Northrop Grumman's maintenance page had no
# controls at all and the gate passed, 2026-10-18). Reasons with this prefix mean "this is not the application".
NOT_APPLICATION = "not an application page:"
SUBMIT_LABEL = re.compile(r"\bsubmit\b|\bsend (my |your |the )?application\b|"
                          r"^(finish|complete|complete (my |your )?application|apply|apply now|apply for this (job|position|role))$", re.I)


def has_submit_control(snapshot: Snapshot) -> bool:
    """A submit control on the page: one the snapshot marked, or any button whose label reads like a submit
    (the model may name a final button the snapshot's exact list does not know)."""
    if snapshot.submit_buttons():
        return True
    for i in snapshot.items:
        buttonish = "submitish" in i or i.get("tag") in ("button", "a") or (i.get("type") or "").lower() in ("submit", "button")
        if buttonish and SUBMIT_LABEL.search(" ".join(str(i.get("label") or "").split())):
            return True
    return False


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


# A picker's own empty text is no value: Electronic Arts' "Secondary Country Code" showed 'Select an option' and the gate
# asked for a source for it, which the run could not give (2026-10-21).
PLACEHOLDER_VALUE = re.compile(r"^(?:(?:please )?(?:select|choose|pick)(?: an?| one| your)?(?: option| item| value| one| country)?"
                               r"(?:\.\.\.|…)?|no selection|none selected|not selected|--+)$", re.I)


# A "required / please complete" message beside a field that now holds a real value is stale: SAP SuccessFactors keeps
# "Please complete your personal information." beside Salutation after 'Mr' is chosen, until its own next validation, and
# Westinghouse's run parked over it twice with the rest of the form unfilled (2026-10-21). Format errors stay.
_REQUIRED_MSG = re.compile(r"\b(required|is mandatory|please (complete|select|enter|provide|fill|choose)|cannot be (blank|empty)|"
                           r"must not be (blank|empty)|this info is required|missing)\b", re.I)


_SECTION_MSG = re.compile(r"\bplease complete (your |the )?(personal |contact |profile )?(information|details|profile|section)\b", re.I)
_FORMAT_MSG = re.compile(r"\b(valid|invalid|format|must be|at least|at most|characters?|match(es)?|correct|too (long|short))\b", re.I)


# The site's display-language picker is page chrome, not a question: SAP SuccessFactors shows "Language = English US
# (English US)" above L3Harris's form, the gate demanded a sourced decision for it, and every pick reloaded the page
# ("Execution context was destroyed"), so the run parked (2026-10-21). A locale name repeated in brackets marks it.
_LOCALE_REPEAT = re.compile(r"^\s*([^()]+?)\s*[\u200e\u200f]*\(\s*[\u200e\u200f]*\1\s*[\u200e\u200f]*\)\s*[\u200e\u200f]*\s*$")


# A cookie-consent control is page chrome too: Qualcomm's OneTrust button "Cookies Settings, Opens the preference center
# dialog" reads as a dropdown holding 'Cookies Settings', and the gate demanded a sourced decision for it (2026-10-21).
COOKIE_CONTROL = re.compile(r"\bcookies?\b.{0,40}\b(settings|preferences|policy|consent|choices|notice)\b|"
                            r"\b(manage|customi[sz]e|accept|reject)\b.{0,20}\bcookies?\b", re.I)


# A job-search page's own sorting and paging pickers are page chrome too (UKG, 2026-10-21: "Sort by = Latest" blocked a
# park on the careers search page).
SEARCH_CONTROL = re.compile(r"^(sort( results)?( by)?|order( by)?|results per page|items per page|jobs per page|per page|"
                            r"page size)\s*:?\s*$", re.I)


def is_display_language_picker(label: str, value: str) -> bool:
    lab = re.sub(r"[\s:*]+$", "", (label or "").strip()).lower()
    return lab in ("language", "select language", "change language", "site language", "display language") \
        and bool(_LOCALE_REPEAT.match(value or ""))


def drop_stale_required(errors: dict | None, snapshot) -> dict:
    """The page's error scan without 'required' messages whose own field now holds a value (and is not marked invalid)."""
    from . import answers as _answers
    errs = dict(errors or {})
    if not errs:
        return errs
    filled = {}
    for it in getattr(snapshot, "items", []) or []:
        val = str(it.get("shown") or it.get("value") or "").strip()
        if it.get("type") in ("checkbox", "radio") or not val or PLACEHOLDER_VALUE.match(val) or it.get("invalid") is True:
            continue
        lab = _answers.norm(it.get("label") or "")
        if lab:
            filled[lab] = val

    # A section-level "Please complete your personal information." names no field (L3Harris, 2026-10-21: it sat by the
    # 'Login/Primary Email' heading while the box itself is '* E-Mail'); with no empty required field left on the page it
    # is stale too.
    nothing_empty = not errs.get("empty_required")

    def stale(e: dict) -> bool:
        near = _answers.norm(e.get("near") or "")
        text = str(e.get("text") or "")
        if nothing_empty and _SECTION_MSG.search(text):
            return True
        return bool(near) and bool(_REQUIRED_MSG.search(text)) and not _FORMAT_MSG.search(text) and near in filled

    for kind in ("error_text", "alerts"):
        if errs.get(kind):
            errs[kind] = [e for e in errs[kind] if not stale(e)]
    return errs


def _stored_answer_matching(item: dict, value: str, company: str, portal: str) -> str | None:
    """The key of his stored answer to this field's question (looked up by its label) when the page already shows it."""
    from . import answers as _answers
    from . import readback as _rb
    label = str(item.get("label") or "").strip()
    if len(label) < 6:
        return None
    try:
        a = _answers.lookup(label, company, portal)
    except Exception:
        return None
    if a is None or not a.has_value or a.answer is None:
        return None
    wants = [w.strip() for w in str(a.answer).split(";") if w.strip()]
    for w in wants[:1] if a.answer_type in ("choice_chain",) else wants:
        try:
            if _rb.values_equal(w, value, _rb._kind_for(item, w), label):
                return a.key
        except Exception:
            if _answers.norm(w) == _answers.norm(value):
                return a.key
    return None


# Eightfold's "Please review before continuing" consent step before an account exists (Qualcomm, 2026-10-21): its Submit
# creates the account; the application comes after it.
ACCOUNT_PAGE = re.compile(r"\b(please review before continuing|password help|forgot (your )?password|reset (your )?password|change (your )?password|"
                          r"create (an |your )?account|candidate (sign[- ]?in|login)|career opportunities: sign in|"
                          r"(^|\| )(sign[- ]?in|log[- ]?in|register)( \||$))", re.I)


_FIELDS_TO_FIX = re.compile(r"(?m)^[ \t]*(.{1,90}?)[ \t]*\n+[ \t]*Fields to fix:?[ \t]*(\d+)")


def fields_to_fix(page_text: str) -> list[tuple[str, int]]:
    """Oracle entry tiles that still miss a required field: (the tile's last title line, how many fields)."""
    out = []
    for m in _FIELDS_TO_FIX.finditer((page_text or "").replace("\u200b", "")):
        n = int(m.group(2))
        if n > 0:
            out.append((m.group(1).strip(), n))
    return out


def run_gate(*, job: dict, snapshot: Snapshot, page_text: str, expected_resume: str | None,
             free_text_answers: list[str], portal: str, page_no: int = 0) -> GateResult:
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
    # ...nor is a password-help page, which has no password box (CenturyLink's SuccessFactors 'Password Help', 2026-10-21:
    # the run pressed its Submit through submit and the gate passed; the judge caught it)
    heads = " | ".join([snapshot.title or ""] + list(snapshot.headings or [])[:4])
    if ACCOUNT_PAGE.search(heads):
        return GateResult("blocked", [f"this is an account page ({heads[:80]!r}), not the application form: finish signing in "
                                      "(credentials / the password reset), then fill the application"])

    # 2. challenge
    if snapshot.challenge.get("challenge"):
        return GateResult("captcha_present", [f"human-verification challenge: {', '.join(snapshot.challenge.get('kinds') or [])}"])


    # 3. page errors and required fields
    # Workday's Review page lists every section; "No Response" under Work Experience (he has four roles) or
    # under Education's Field of Study means a section was skipped: go back and fill it (2026-10-18).
    # The rail names a "Review" step on every page: only the Review page itself has that heading / current step.
    on_review = any(h.strip().lower() == "review" for h in (snapshot.headings or [])) \
        or bool(re.search(r"current step \d+ of \d+\s*\n\s*Review\b", page_text[:4000], re.I))
    if (portal or "").lower() == "workday" and on_review:
        gaps = [sec for sec in ("Work Experience", "Professional Experience", "Work History", "Employment History", "Field of Study")
                if re.search(sec + r"\s*\n\s*No Response", page_text)]
        if gaps:
            return GateResult("blocked", [f"the Review page shows '{g}: No Response': call go_to_step('My Experience'), then fill_sections, "
                                          "then Save and Continue forward to Review" for g in gaps])
        # English is his language: the Languages section's fluency box must be ticked (American Family, 2026-10-18).
        if re.search(r"I am fluent in this language\W{0,3}\s*No\b", page_text, re.I):
            return GateResult("blocked", ["the Review page shows 'I am fluent in this language: No' for English: call go_to_step('My Experience'), "
                                          "tick 'I am fluent in this language' (answer_key experience.fluent_in_english), then Save and Continue forward to Review"])
        from .adapters.workday_sections import review_gaps
        missing = review_gaps(page_text, job.get("company", ""))
        if missing:
            from .adapters.workday_sections import LANGUAGES_GAP
            return GateResult("blocked", [
                (f"the Review page shows Languages: No Response: call go_to_step('My Experience'), click the Languages section's "
                 "Add button, choose English for Language (answer_key background.languages_spoken), tick 'I am fluent in this "
                 "language' (experience.fluent_in_english), set each proficiency box with answer_key experience.english_proficiency, "
                 "then Save and Continue forward to Review")
                if m == LANGUAGES_GAP else
                f"the Review page does not show {m}: call go_to_step('My Experience'), then fill_sections, then Save and Continue forward to Review"
                for m in missing[:4]])
    # Oracle keeps each Experience / Education entry as a tile, and a tile missing a required field shows "Fields to fix: N";
    # its SUBMIT then does nothing (Texas Instruments, 2026-10-20: two tiles from Oracle's résumé import, 'Computer Science'
    # and 'Engineering Honors / Texas A&M University', and two inert submits spent the run).
    tiles = fields_to_fix(page_text)
    if tiles:
        return GateResult("blocked", [
            f"the tile '{t}' shows 'Fields to fix: {n}': open it with its Edit (pencil) button and fill the missing required "
            "field from the stored answers (education.* / experience.work.N.*), then save the tile; a tile that matches none of "
            "his stored entries (Oracle's résumé import adds such tiles) is removed with its Delete button instead"
            for t, n in tiles[:4]])
    # Greenhouse's single page: when it has an Employment section, every stored work entry must be in it.
    if (portal or "").lower() == "greenhouse":
        from .adapters.greenhouse_sections import review_gaps_from_snapshot
        missing = review_gaps_from_snapshot(snapshot, job.get("company", ""))
        if missing:
            return GateResult("blocked", [f"the Employment section does not show {m}: call fill_sections, then submit again" for m in missing[:4]])
    why = why_blocked(drop_stale_required(snapshot.errors, snapshot))
    reasons += why

    # 4. decisions: every value has a source; attestations only from standing answers
    decs = records.decisions(job["id"])
    # Only the newest decision on a control counts: Axiom Space's NASA export-screening box was set first without an
    # answer key (source adapter), then again from his consent rule under another label, and the stale first decision
    # kept the gate blocked (2026-10-19). A control is its ref on its page (Workday reuses refs across pages).
    newest: dict[tuple, int] = {}
    for i, d in enumerate(decs):
        if d.get("ref"):
            newest[(d.get("page"), d.get("ref"))] = i
    for i, d in enumerate(decs):
        if d.get("ref") and newest.get((d.get("page"), d.get("ref"))) != i:
            continue
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
        if not shown and expected_resume and expected_resume.lower() in (page_text or "").lower():
            # Oracle empties its import input and lists the uploaded file as text beside 'Remove Resume' (onsemi and
            # Vertiv were held on an attached résumé, 2026-10-19)
            shown = expected_resume.lower()
        if not shown:
            reasons.append("resume not attached: the Resume/CV slot is empty (call upload_resume on its ref)")
        elif expected_resume and expected_resume.lower() not in shown:
            reasons.append(f"resume slot holds {shown!r}, expected {expected_resume}")
    elif expected_resume and snapshot.file_inputs():
        shown = " ".join((fi.get("value") or "") for fi in snapshot.file_inputs()).lower()
        if expected_resume.lower() not in shown and expected_resume.lower() not in (page_text or "").lower():
            reasons.append(f"resume not shown on page: expected {expected_resume}")

    # 5b. every value on the page has a sourced decision from THIS attempt (by ref, else by label words).
    from . import readback
    def _nw(s: str) -> set:
        return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 2}
    dec_refs = {d["ref"] for d in decs if d.get("ref")}
    dec_labels = [_nw(d["field_label"]) for d in decs]
    def _sourced(ref: str, label: str) -> bool:
        if ref in dec_refs:
            return True
        lw = _nw(label)
        return any(dl and (dl <= lw or (lw and lw <= dl)) for dl in dec_labels)
    # A value that equals one of his stored answers is sourced by that answer (a résumé autofill or a pick whose
    # read-back came up empty: Ashby's school autocomplete, 2026-10-18), recorded here the way prefill does.
    from .adapters import _standing_values
    try:
        standing = _standing_values()
    except Exception:
        standing = {}
    from . import answers as _answers
    for it in snapshot.items:
        if it.get("tag") not in ("input", "textarea", "select", "dropdown", "richtext", "toggle") or it.get("type") in ("file", "password", "hidden", "submit", "button"):
            continue
        if it.get("disabled"):
            # A disabled control cannot be changed or set (ibotta, 2026-10-18: "Still Student" disables the end-date
            # year, which keeps a default 2026): its value is the portal's, not a decision.
            continue
        val = it.get("shown") or it.get("value") or ""
        if it.get("type") == "checkbox":
            val = "checked" if it.get("checked") else ""
        if it.get("tag") == "toggle":
            val = "pressed" if it.get("pressed") else ""
        if not str(val).strip():
            continue
        # A placeholder is not a value: "--" (IXL's office picker) or a select showing its own question as the
        # selection ("What University do you currently attend?", 2026-10-18).
        if re.fullmatch(r"[\W_]+", str(val)) or PLACEHOLDER_VALUE.match(str(val).strip()) \
                or (it.get("tag") in ("select", "dropdown") or it.get("combo")) and \
                _answers.norm(str(val)) == _answers.norm(it.get("label") or "") and it.get("type") not in ("checkbox", "radio"):
            continue
        if is_display_language_picker(it.get("label") or "", str(val)) or COOKIE_CONTROL.search(it.get("label") or "") \
                or SEARCH_CONTROL.match((it.get("label") or "").strip()):
            continue
        if not _sourced(it.get("ref", ""), it.get("label", "")):
            key = standing.get(_answers.norm(str(val)))
            if not key and it.get("type") not in ("checkbox", "radio") and it.get("tag") != "toggle":
                # A value the portal kept from his earlier application ("Are you willing to relocate? = Yes" on Electronic
                # Arts' Avature form, 2026-10-21) is sourced when it equals his stored answer to that very question; a
                # plain "Yes" matches too many stored answers to be looked up by its text alone.
                key = _stored_answer_matching(it, str(val), job.get("company", ""), portal)
            if not key and any(readback.dial_code_matches(cv, str(val)) for cv, ck in standing.items() if ck == "identity.country"):
                key = "identity.country"      # a phone country picker showing "+1" for his United States (Epic Games)
            if key and it.get("type") not in ("checkbox", "radio") and it.get("tag") != "toggle":
                records.record_decision(job["id"], page_no, it.get("label") or "", str(val)[:300], f"standing:{key}", it.get("ref", ""))
                dec_refs.add(it.get("ref", ""))
                continue
            reasons.append(f"value on page with no sourced decision this attempt: [{it.get('ref')}] {(it.get('label') or '')[:60]} = {str(val)[:40]!r} (re-set it from its answer_key with choose/fill_batch, or clear it)")
    for g in snapshot.groups:
        for o in g.get("options", []):
            if o.get("checked") and not _sourced(o.get("ref", ""), f"{g.get('label', '')} {o.get('label', '')}"):
                reasons.append(f"checked option with no sourced decision this attempt: [{o.get('ref')}] {(g.get('label') or '')[:50]} / {(o.get('label') or '')[:30]} (re-set it with fill_batch using the group's answer_key from lookup_answer, or clear it with value false source 'clear')")

    # 5c. read-back: every decision made on a control still on this page must be what the control shows now.
    reasons += readback.diff(decs, snapshot, page_no)
    for u in (getattr(snapshot, "unsaved", None) or [])[:6]:
        reasons.append(f"the form shows {u.get('shown', '')!r} for '{(u.get('title') or '')[:80]}' but has not stored it (it would be "
                       "missing on submit): tick another option, then this one again, or re-type the text")

    # 6. generated text against guardrails
    for t in free_text_answers:
        blocks, _ = guardrail_check(t)
        if blocks:
            return GateResult("guardrail_block", [f"guardrail {b}" for b in blocks])

    # 7. only a page that can be submitted passes: a maintenance notice or a mid-form page never does
    if not has_submit_control(snapshot):
        perr = portal_error(page_text)
        reasons.append(f"{NOT_APPLICATION} " + (f"the portal shows an error or maintenance notice ({perr[:120]}): park portal_error" if perr else
                       "no submit control on this page: fill it and continue to the final (Review) page; if the portal is broken, park portal_error"))

    if reasons:
        return GateResult("blocked", reasons)
    # Live for a portal Surya switched live, or for the one job he released by id (`goat release N`).
    return GateResult("pass_live" if (SETTINGS.portal_is_live(portal) or bool(job.get("cleared_live"))) else "pass_shadow", [])
