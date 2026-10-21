"""SmartRecruiters one-click form (jobs.smartrecruiters.com/oneclick-ui): Experience and Education are lists of saved
cards, each written through an inline editor. Filled here deterministically, like Workday's My Experience: every stored
work entry and the Texas A&M entry.

Every control is a design-system custom element (spl-*) with its native input or button inside a shadow root:
- 'Add experience entry' / 'Add education entry' ([data-test=add-experience] / [data-test=add-education]) open an
  editor (oc-experience-edit-form / oc-education-edit-form);
- Title, Company and Institution are autocompletes that keep only a value picked from their suggestion list: typed text
  is dropped when the box loses focus, and the first suggestion repeats the typed text;
- From / To are month-year pickers that take 'MM/YYYY' typed into their text box;
- 'I currently work here' / 'I currently attend' are tick boxes; the editor's own Save / Cancel buttons are
  [data-test=experience-save] / [data-test=experience-cancel] (education-save / education-cancel);
- a saved card shows the title, its dates ('2026 - Present') and the company, with Edit and Delete buttons; Delete asks
  'Would you like to delete this entry?' (No / Yes).
CapTech, Wellmark, ServiceNow (2026-10-21): the agent saw none of these buttons and parked every SmartRecruiters form;
the résumé parse had also turned project names into Experience cards with missing dates (Wellmark)."""
from __future__ import annotations

import asyncio
import json
import re

from .. import answers, records
from ..browser import fill
from .workday_sections import SectionReport, _same_company, bullets_to_lines, fit_bullets, stored_work_entries

CARD = {"experience": "oc-experience-entry", "education": "oc-education-entry"}
FORM = {"experience": "oc-experience-edit-form", "education": "oc-education-edit-form"}
ADD = {"experience": '[data-test="add-experience"] button', "education": '[data-test="add-education"] button'}
SAVE = {"experience": '[data-test="experience-save"] button', "education": '[data-test="education-save"] button'}
CANCEL = {"experience": '[data-test="experience-cancel"] button', "education": '[data-test="education-cancel"] button'}
DELETE = {"experience": '[data-test="experience-entry-delete"] button', "education": '[data-test="education-entry-delete"] button'}
DIALOG = "spl-dialog[open]"
WAIT_MS = 6000          # a control that is not there is skipped or reported, never waited on for 30 s (Wellmark's
                        # Education editor has no dates, 2026-10-21)
DIALOG_YES = 'spl-dialog[open] spl-button[type="primary"] button'

JS_CARDS = r"""
(kind) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  return JSON.stringify(Array.from(document.querySelectorAll(kind === 'experience' ? 'oc-experience-entry' : 'oc-education-entry')).map((c, i) => {
    const q = (dt) => c.querySelector('[data-test="' + kind + '-entry-' + dt + '"]');
    const date = norm(q('date') && q('date').innerText);
    const head = kind === 'experience' ? q('title') : q('institution');
    let name = norm(head && head.innerText);
    if (date && name.endsWith(date)) name = norm(name.slice(0, name.length - date.length));
    return { idx: i, name, date, company: norm(q('company') && q('company').innerText), major: norm(q('major') && q('major').innerText),
             degree: norm(q('degree') && q('degree').innerText), editing: !!c.querySelector('oc-experience-edit-form, oc-education-edit-form') };
  }));
}
"""

# The visible suggestions of the autocomplete [data-test=tid] inside the open editor, with their centres.
JS_OPTIONS = r"""
([form, tid]) => {
  const f = document.querySelector(form);
  const h = f && f.querySelector('[data-test="' + tid + '"]');
  if (!h || !h.shadowRoot) return '[]';
  return JSON.stringify(Array.from(h.shadowRoot.querySelectorAll('spl-select-option')).map(o => {
    const r = o.getBoundingClientRect();
    return { txt: (o.innerText || o.textContent || '').replace(/\s+/g, ' ').trim(), x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), w: r.width, h: r.height };
  }).filter(o => o.w > 0 && o.h > 0 && o.txt));
}
"""

# What the editor's controls hold: the component's own value (not the text in its box), the date boxes' text, the tick.
JS_EDITOR = r"""
(form) => {
  const f = document.querySelector(form);
  if (!f) return JSON.stringify({ open: false });
  const val = (h) => { if (!h) return ''; let v = h.value; if (v && typeof v === 'object') v = v.displayString || v.text || v.name || ''; return v == null ? '' : String(v); };
  const deepInput = (h) => { const walk = (r) => { for (const c of r.querySelectorAll('*')) { if ((c.tagName === 'INPUT' && (c.getAttribute('type') || 'text') === 'text') || c.tagName === 'TEXTAREA') return c; if (c.shadowRoot) { const x = walk(c.shadowRoot); if (x) return x; } } return null; }; return h && h.shadowRoot ? walk(h.shadowRoot) : null; };
  const out = { open: true, fields: {} };
  for (const dt of ['job-title-autocomplete', 'company-autocomplete', 'institution-autocomplete']) { const h = f.querySelector('[data-test="' + dt + '"]'); if (h) out.fields[dt] = val(h); }
  for (const dt of ['education-major', 'education-degree']) { const w = f.querySelector('[data-test="' + dt + '"]'); const h = w && (w.matches('spl-input') ? w : w.querySelector('spl-input')); const i = deepInput(h); if (w) out.fields[dt] = i ? i.value : val(h); }
  for (const dt of ['experience-date-from', 'experience-date-to', 'education-date-from', 'education-date-to']) {
    const w = f.querySelector('[data-test="' + dt + '"]'); if (!w) continue;
    const h = w.matches('spl-date-field') ? w : w.querySelector('spl-date-field'); const i = deepInput(h);
    out.fields[dt] = { text: i ? i.value : '', set: !!(h && h.value) };
  }
  const cb = f.querySelector('spl-checkbox'); out.current = !!(cb && cb.checked);
  const ta = f.querySelector('spl-textarea'); const ti = deepInput(ta); out.description = ti ? ti.value : val(ta); out.maxlength = ti && ti.maxLength > 0 ? ti.maxLength : 0;
  // the editor's messages ('Please provide start date', 'This field is required')
  const msgs = []; const walk = (r) => { for (const c of r.querySelectorAll('*')) { const cls = (c.className && c.className.baseVal === undefined ? String(c.className) : '') + ' ' + (c.getAttribute('role') || '');
      if (/error|invalid|alert/i.test(cls) && c.children.length === 0) { const t = (c.innerText || c.textContent || '').replace(/\s+/g, ' ').trim(); if (t && t.length < 160 && !msgs.includes(t)) msgs.push(t); }
      if (c.shadowRoot) walk(c.shadowRoot); } };
  walk(f); out.errors = msgs.slice(0, 6);
  return JSON.stringify(out);
}
"""

JS_DIALOG_TEXT = r"""() => { const d = document.querySelector('spl-dialog[open]'); return d ? (d.innerText || d.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 200) : ''; }"""


def month_year(value: str) -> str:
    """'02/01/2026', 'August 2025', 'May 2028' -> 'MM/YYYY' ('' when there is no month and year)."""
    d = fill.parse_date(value or "")
    return f"{d['month']}/{d['year']}" if d.get("month") and d.get("year") else ""


def _year(value: str) -> str:
    return fill.parse_date(value or "").get("year", "")


def _same_text(a: str, b: str) -> bool:
    na, nb = answers.norm(a), answers.norm(b)
    return bool(na and nb) and (na == nb or na.startswith(nb) or nb.startswith(na))


class SmartRecruitersSections:
    def __init__(self, ex, job: dict, page_no: int = 0):
        self.ex, self.job, self.page_no = ex, job, page_no
        self.page = ex.page
        self.company = job.get("company", "") or ""
        self.rep = SectionReport()

    # ------------------------------------------------------------------ reading
    async def applies(self) -> bool:
        try:
            return bool(await self.page.locator("oc-experience, oc-education").count())
        except Exception:
            return False

    async def cards(self, kind: str) -> list[dict]:
        return json.loads(await self.page.evaluate(JS_CARDS, kind) or "[]")

    async def editor(self, kind: str) -> dict:
        return json.loads(await self.page.evaluate(JS_EDITOR, FORM[kind]) or "{}")

    def _answer(self, key: str) -> str:
        a = answers.get(key, self.company, "smartrecruiters")
        return str(a.answer).strip() if a and a.has_value and a.answer is not None else ""

    # ------------------------------------------------------------------ primitives
    async def _has(self, selector: str) -> bool:
        try:
            return bool(await self.page.locator(selector).count())
        except Exception:
            return False

    async def _type_into(self, selector: str, text: str, delay: int = 25) -> None:
        box = self.page.locator(selector).first
        await box.click(timeout=WAIT_MS)
        await self.page.keyboard.press("Control+a")
        await self.page.keyboard.press("Backspace")
        if text:
            await self.page.keyboard.type(text, delay=delay)

    async def _pick(self, kind: str, tid: str, text: str) -> str:
        """Type into the autocomplete and click the suggestion that says `text` (the first suggestion repeats what was
        typed): -> the value the component now holds ('' when nothing was picked)."""
        await self._type_into(f'{FORM[kind]} [data-test="{tid}"] input', text)
        want, opts = answers.norm(text), []
        for _ in range(20):
            await asyncio.sleep(0.2)
            opts = json.loads(await self.page.evaluate(JS_OPTIONS, [FORM[kind], tid]) or "[]")
            if any(answers.norm(o["txt"]) == want for o in opts):
                break
        hit = next((o for o in opts if answers.norm(o["txt"]) == want), None)
        if hit is None:
            await self.page.keyboard.press("Escape")
            return ""
        await self.page.mouse.click(hit["x"], hit["y"])
        for _ in range(12):
            await asyncio.sleep(0.15)
            got = (await self.editor(kind)).get("fields", {}).get(tid, "")
            if got:
                return got
        return ""

    async def _month_year(self, kind: str, which: str, value: str) -> bool | None:
        """True when the box took the date, False when it did not, None when this form has no such box."""
        dt = f"{kind}-date-{which}"
        sel = f'{FORM[kind]} [data-test="{dt}"] input[type="text"]'
        if not await self._has(sel):
            return None
        mmyyyy = month_year(value)
        if not mmyyyy:
            return False
        await self._type_into(sel, mmyyyy, delay=40)
        # Enter commits the typed month (the picker parses it then); Escape alone left Wellmark's From empty, and every
        # entry failed with 'From was not taken' (2026-10-21). Escape then closes the calendar.
        await self.page.keyboard.press("Enter")
        await asyncio.sleep(0.3)
        await self.page.keyboard.press("Escape")
        for _ in range(15):
            await asyncio.sleep(0.2)
            st = (await self.editor(kind)).get("fields", {}).get(dt) or {}
            if st.get("set") and st.get("text") == mmyyyy:
                return True
        return False

    async def _tick(self, kind: str) -> bool | None:
        sel = f"{FORM[kind]} spl-checkbox input[type=\"checkbox\"]"
        if not await self._has(sel):
            return None
        if not (await self.editor(kind)).get("current"):
            await self.page.locator(sel).first.click(timeout=WAIT_MS)
        for _ in range(10):
            if (await self.editor(kind)).get("current"):
                return True
            await asyncio.sleep(0.15)
        return False

    async def _confirm_dialog(self, pattern: str) -> bool:
        """Answer Yes to the portal's own confirmation ('Would you like to delete this entry?') when it says `pattern`."""
        for _ in range(10):
            await asyncio.sleep(0.2)
            text = await self.page.evaluate(JS_DIALOG_TEXT)
            if text:
                if re.search(pattern, text, re.I):
                    await self.page.locator(DIALOG_YES).first.click(timeout=WAIT_MS)
                    await asyncio.sleep(0.6)
                    return True
                return False
        return False

    async def _cancel(self, kind: str) -> None:
        loc = self.page.locator(CANCEL[kind]).first
        if await loc.count():
            await loc.click(timeout=WAIT_MS)
            await asyncio.sleep(0.5)
            await self._confirm_dialog(r"discard|lose|unsaved|cancel|leave")

    async def _open(self, kind: str) -> bool:
        if not await self._has(ADD[kind]):
            return False
        await self.page.locator(ADD[kind]).first.click(timeout=WAIT_MS)
        for _ in range(20):
            await asyncio.sleep(0.15)
            if (await self.editor(kind)).get("open"):
                return True
        return False

    async def _save(self, kind: str) -> tuple[bool, list[str]]:
        await self.page.locator(SAVE[kind]).first.click(timeout=WAIT_MS)
        for _ in range(25):
            await asyncio.sleep(0.2)
            st = await self.editor(kind)
            if not st.get("open"):
                return True, []
        return False, (await self.editor(kind)).get("errors") or ["the editor stayed open after Save"]

    async def _delete(self, kind: str, idx: int) -> bool:
        before = len(await self.cards(kind))
        await self.page.locator(CARD[kind]).nth(idx).locator(DELETE[kind]).first.click(timeout=WAIT_MS)
        await self._confirm_dialog(r"delete")
        for _ in range(15):
            await asyncio.sleep(0.2)
            if len(await self.cards(kind)) < before:
                return True
        return False

    # ------------------------------------------------------------------ work entries
    async def add_work(self, ent: dict) -> tuple[bool, str]:
        try:
            return await self._add_work(ent)
        except Exception as e:  # noqa: BLE001 - reported per entry; the next entry still runs
            try:
                await self._cancel("experience")
            except Exception:
                pass
            return False, f"{type(e).__name__}: {str(e).splitlines()[0][:120]}"

    async def _add_work(self, ent: dict) -> tuple[bool, str]:
        n, form = ent["n"], FORM["experience"]
        if not await self._open("experience"):
            return False, "the Add experience editor did not open"
        title = await self._pick("experience", "job-title-autocomplete", ent["title"])
        company = await self._pick("experience", "company-autocomplete", ent["company"])
        if answers.norm(title) != answers.norm(ent["title"]) or answers.norm(company) != answers.norm(ent["company"]):
            await self._cancel("experience")
            return False, f"Title / Company were not taken (title {title!r}, company {company!r})"
        if await self._month_year("experience", "from", ent["start"]) is False:
            await self._cancel("experience")
            return False, f"From was not taken ({ent['start']!r})"
        if ent["current"]:
            if await self._tick("experience") is False:
                await self._cancel("experience")
                return False, "'I currently work here' did not tick"
        elif ent["end"] and await self._month_year("experience", "to", ent["end"]) is False:
            await self._cancel("experience")
            return False, f"To was not taken ({ent['end']!r})"
        desc = bullets_to_lines(ent["description"]) if ent["description"] else ""
        if desc:
            cap = (await self.editor("experience")).get("maxlength") or 0
            if cap and len(desc) > cap:
                desc = fit_bullets(desc, cap)
            await self._type_into(f"{form} spl-textarea textarea", desc, delay=2)
        ok, errs = await self._save("experience")
        if not ok:
            await self._cancel("experience")
            return False, "Save was refused: " + "; ".join(errs)[:200]
        for f, v in (("title", ent["title"]), ("company", ent["company"]), ("start", month_year(ent["start"])),
                     ("end", "" if ent["current"] else month_year(ent["end"])), ("current", "Yes" if ent["current"] else ""),
                     ("description", desc)):
            if v:
                records.record_decision(self.job["id"], self.page_no, f"Experience {ent['company']}: {f}", v,
                                        f"standing:experience.work.{n}.{f}", "")
        return True, ""

    async def experience(self) -> None:
        rep, stored = self.rep, stored_work_entries(self.company, "smartrecruiters")
        if not stored:
            rep.skipped.append("experience: no stored work entries")
            return
        keep: dict[int, int] = {}
        doomed: list[tuple[dict, str]] = []
        for c in await self.cards("experience"):
            if c["editing"]:
                continue
            ent = next((e for e in stored if _same_company(c["company"], e["company"])), None)
            if ent is None:
                doomed.append((c, "not one of his stored entries"))
            elif ent["n"] in keep:
                doomed.append((c, f"a second card for {ent['company']}"))
            elif not c["date"] or (_year(ent["start"]) and _year(ent["start"]) not in c["date"]) or not _same_text(c["name"], ent["title"]):
                doomed.append((c, "title or dates differ from his stored entry"))
            else:
                keep[ent["n"]] = c["idx"]
                rep.present.append(f"experience: {ent['company']}")
        for c, why in sorted(doomed, key=lambda t: -t[0]["idx"]):
            label = c["name"] or c["company"] or "(empty card)"
            if await self._delete("experience", c["idx"]):
                rep.updated.append(f"experience card '{label}' removed: {why}")
            else:
                rep.failed.append(f"experience card '{label}' could not be deleted ({why})")
        for ent in stored:
            if ent["n"] in keep:
                continue
            ok, why = await self.add_work(ent)
            if ok:
                rep.added.append(f"experience: {ent['title']} at {ent['company']}")
            else:
                rep.failed.append(f"experience: {ent['company']}: {why}")

    # ------------------------------------------------------------------ education
    async def add_school(self, school: str) -> tuple[bool, str]:
        try:
            return await self._add_school(school)
        except Exception as e:  # noqa: BLE001 - reported, never raised
            try:
                await self._cancel("education")
            except Exception:
                pass
            return False, f"{type(e).__name__}: {str(e).splitlines()[0][:120]}"

    async def _add_school(self, school: str) -> tuple[bool, str]:
        form = FORM["education"]
        major, degree = self._answer("education.major"), self._answer("education.degree")
        start, grad = self._answer("education.start"), self._answer("education.graduation")
        if not await self._open("education"):
            return False, "the Add education editor did not open"
        got = await self._pick("education", "institution-autocomplete", school)
        if answers.norm(got) != answers.norm(school):
            await self._cancel("education")
            return False, f"Institution was not taken ({got!r})"
        if major and await self._has(f'{form} [data-test="education-major"] input'):
            await self._type_into(f'{form} [data-test="education-major"] input', major)
        if degree and await self._has(f'{form} [data-test="education-degree"] input'):
            await self._type_into(f'{form} [data-test="education-degree"] input', degree)
        if await self._month_year("education", "from", start) is False:
            await self._cancel("education")
            return False, f"From was not taken ({start!r})"
        if grad and await self._month_year("education", "to", grad) is False:
            await self._cancel("education")
            return False, f"To was not taken ({grad!r})"
        st = (await self.editor("education")).get("fields", {})
        if (major and answers.norm(st.get("education-major", "")) != answers.norm(major)) or \
                (degree and answers.norm(st.get("education-degree", "")) != answers.norm(degree)):
            await self._cancel("education")
            return False, "Major / Degree were not taken"
        ok, errs = await self._save("education")
        if not ok:
            await self._cancel("education")
            return False, "Save was refused: " + "; ".join(errs)[:200]
        for f, key, v in (("institution", "education.school", school), ("major", "education.major", major),
                          ("degree", "education.degree", degree), ("from", "education.start", month_year(start)),
                          ("to", "education.graduation", month_year(grad))):
            if v:
                records.record_decision(self.job["id"], self.page_no, f"Education {school}: {f}", v, f"standing:{key}", "")
        return True, ""

    async def education(self) -> None:
        rep = self.rep
        school = self._answer("education.school")
        if not school:
            rep.skipped.append("education: no stored school")
            return
        names = [school] + [v.strip() for v in self._answer("education.school_variants").split(";") if v.strip()]
        high_school = self._answer("education.high_school")
        major = self._answer("education.major")
        years = [y for y in (_year(self._answer("education.start")), _year(self._answer("education.graduation"))) if y]
        mine, doomed = None, []
        for c in await self.cards("education"):
            if c["editing"]:
                continue
            if any(_same_text(c["name"], v) for v in names):
                if mine is None and (not c["date"] or all(y in c["date"] for y in years)) and (not major or not c["major"] or _same_text(c["major"], major)):
                    mine = c
                    rep.present.append(f"education: {school}")
                else:
                    doomed.append((c, "a second card, or dates / major differ from his stored entry"))
            elif high_school and _same_text(c["name"], high_school):
                continue          # his high school, parsed from the résumé: true, left alone
            else:
                doomed.append((c, "not one of his schools"))
        for c, why in sorted(doomed, key=lambda t: -t[0]["idx"]):
            label = c["name"] or "(empty card)"
            if await self._delete("education", c["idx"]):
                rep.updated.append(f"education card '{label}' removed: {why}")
            else:
                rep.failed.append(f"education card '{label}' could not be deleted ({why})")
        if mine is None:
            ok, why = await self.add_school(school)
            if ok:
                rep.added.append(f"education: {school}")
            else:
                rep.failed.append(f"education: {school}: {why}")

    # ------------------------------------------------------------------
    async def run(self) -> SectionReport:
        if not await self.applies():
            self.rep.skipped.append("no SmartRecruiters Experience / Education section on this page")
            return self.rep
        # an editor left open (by the model or an earlier run) is closed first; its entry is redone from the store
        for kind in ("experience", "education"):
            try:
                if (await self.editor(kind)).get("open"):
                    await self._cancel(kind)
            except Exception:
                pass
        for kind, step in (("experience", self.experience), ("education", self.education)):
            try:
                await step()
            except Exception as e:  # noqa: BLE001 - one section's trouble never stops the other
                self.rep.failed.append(f"{kind}: {type(e).__name__}: {str(e).splitlines()[0][:120]}")
        return self.rep
