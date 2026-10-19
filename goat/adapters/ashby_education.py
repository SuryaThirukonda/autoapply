"""Ashby's system field "Education History" (`_systemfield_education_history`): School (a search), Field of Study, End
Date (month and year selects) and "Still Student?". Surya is a current student, so the entry is his school, his
major and "Still Student?" ticked with the end date left empty.

Why deterministic: ibotta (2026-10-19) rejected the field as "Invalid value" after the agent picked an end month,
found no 2028 in a year list that stops at 2027, and then ticked "Still Student?". Ticking it disables the selects
but keeps their values, so the field carried isCurrent with an end date of May 2026."""
from __future__ import annotations

import logging

from .. import answers, records
from ..browser import fill

log = logging.getLogger("goat.adapters.ashby_education")

JS_ASHBY_EDUCATION = r"""() => {
  const entry = document.querySelector('[data-field-path="_systemfield_education_history"]');
  if (!entry) return JSON.stringify(null);
  const ref = el => el ? (el.getAttribute('data-goat-ref') || '') : '';
  const school = entry.querySelector('input[role="combobox"], input[placeholder*="school" i]');
  const major = entry.querySelector('input[id*="-major"]');
  const dates = entry.querySelector('[id*="-endDate"]');
  const sels = dates ? Array.from(dates.querySelectorAll('select')) : [];
  const cur = entry.querySelector('input[type="checkbox"][id*="-isCurrent"]');
  return JSON.stringify({
    school: ref(school), school_value: school ? (school.value || '') : '',
    major: ref(major), major_value: major ? (major.value || '') : '',
    month: ref(sels[0]), month_value: sels[0] ? (sels[0].value || '') : '',
    year: ref(sels[1]), year_value: sels[1] ? (sels[1].value || '') : '',
    current: ref(cur), current_checked: !!(cur && cur.checked),
  });
}"""

LABEL = "Education History"


def _parts(key: str) -> list[str]:
    a = answers.get(key)
    return [w.strip() for w in str(a.answer).split(";") if w.strip()] if a and a.has_value else []


async def fill_education(ex, job: dict, page_no: int) -> dict | None:
    """Fill the first Education History block. None when the page has no such field (or its controls have no
    refs yet); else a report {set, failed, kept, errors}."""
    info = await ex.eval(JS_ASHBY_EDUCATION)
    if not isinstance(info, dict) or not info.get("school"):
        return None
    rep: dict = {"set": 0, "failed": 0, "kept": 0, "errors": []}
    jid = job.get("id")

    def done(label: str, value: str, key: str, ref: str) -> None:
        records.record_decision(jid, page_no, f"{LABEL} / {label}", value, f"standing:{key}", ref)

    # School: the answer itself first, then the portal spellings
    wants = _parts("education.school")
    wants += [w for w in _parts("education.school_variants") if w not in wants]
    if wants:
        shown = info.get("school_value") or ""
        if shown and any(answers.norm(shown) == answers.norm(w) for w in wants):
            rep["kept"] += 1
            done("School", shown, "education.school", info["school"])
        else:
            r = await fill.choose(ex, info["school"], wants)
            if r.ok:
                rep["set"] += 1
                done("School", r.readback, "education.school", info["school"])
            else:
                rep["failed"] += 1
                rep["errors"].append(f"School: {r.error[:160]}")

    # Field of Study
    major = next(iter(_parts("education.major")), "")
    if major and info.get("major"):
        if answers.norm(info.get("major_value") or "") == answers.norm(major):
            rep["kept"] += 1
            done("Field of Study", major, "education.major", info["major"])
        else:
            r = (await fill.fill_batch(ex, [{"ref": info["major"], "value": major}]))[0]
            if r.ok:
                rep["set"] += 1
                done("Field of Study", r.readback, "education.major", info["major"])
            else:
                rep["failed"] += 1
                rep["errors"].append(f"Field of Study: {r.error[:160]}")

    # Still a student: no end date, "Still Student?" ticked. A ticked box disables the selects but keeps their
    # values, so untick it first when an end date is left over, clear both selects, then tick it again.
    if info.get("current"):
        if info.get("current_checked") and (info.get("month_value") or info.get("year_value")):
            await fill.fill_batch(ex, [{"ref": info["current"], "value": False}])
            info = await ex.eval(JS_ASHBY_EDUCATION) or info
        clear = [{"ref": info[k], "value": ""} for k in ("month", "year") if info.get(k) and info.get(k + "_value")]
        if clear:
            for r in await fill.fill_batch(ex, clear):
                if not r.ok:
                    rep["errors"].append(f"End Date not cleared: {r.error[:120]}")
        if not info.get("current_checked") or clear:
            r = (await fill.fill_batch(ex, [{"ref": info["current"], "value": True}]))[0]
            ok = r.ok
        else:
            ok = True
        after = await ex.eval(JS_ASHBY_EDUCATION)
        if ok and isinstance(after, dict) and after.get("current_checked") and not after.get("month_value") and not after.get("year_value"):
            rep["set"] += 1
            done("Still Student?", "checked", "education.graduation_month_year", info["current"])
        else:
            rep["failed"] += 1
            rep["errors"].append("Still Student? / End Date: could not leave the box ticked with the end date empty")
    return rep
