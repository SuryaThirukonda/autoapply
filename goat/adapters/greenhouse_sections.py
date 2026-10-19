"""Greenhouse job-boards employment section, filled the Simplify way (same engine as Workday's).

The React form (job-boards.greenhouse.io/embed/job_app) gives every employment block stable ids:
company-name-N, title-N, start-date-month-N (a react-select box), start-date-year-N, end-date-month-N,
end-date-year-N (both disabled while the "Current role" checkbox current-role-N_1 is ticked), a
"Remove employment" button per block and one "Add another" button after the last block. No location,
description or websites here (LinkedIn / website are plain fields the generic prefill handles)."""
from __future__ import annotations

import asyncio
import re

from .. import answers
from ..browser import fill
from .workday_sections import WorkdaySections

# Greenhouse ids -> the field names the shared filler already understands.
_NAMES = {"company-name": "companyName", "title": "jobTitle", "current-role": "currentlyWorkHere",
          "start-date-month": "startMonthCombo", "start-date-year": "startYear",
          "end-date-month": "endMonthCombo", "end-date-year": "endYear"}

JS_GH_SECTIONS = r"""
() => {
  let counter = 0;
  for (const el of document.querySelectorAll('[data-goat-ref]')) {
    const n = parseInt((el.getAttribute('data-goat-ref') || 'e0').slice(1), 10); if (n > counter) counter = n;
  }
  const mkref = (el) => { let r = el.getAttribute('data-goat-ref'); if (!r) { r = 'e' + (++counter); el.setAttribute('data-goat-ref', r); } return r; };
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const NAMES = %NAMES%;
  const entries = {};
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const m = /^(company-name|title|start-date-month|start-date-year|end-date-month|end-date-year|current-role)-(\d+)(?:_\d+)?$/.exec(el.id || '');
    if (!m) continue;
    const key = 'employment-' + m[2];
    const e = entries[key] || (entries[key] = { kind: 'employment', idx: parseInt(m[2], 10), fields: {} });
    const tag = el.tagName.toLowerCase();
    const type = tag === 'select' ? 'select' : (el.getAttribute('type') || tag).toLowerCase();
    let value = el.value || '';
    if (type === 'checkbox') value = el.checked ? 'checked' : '';
    else if (tag === 'select') value = el.selectedOptions.length ? norm(el.selectedOptions[0].textContent) : '';
    else if (/month$/.test(m[1]) && !value) {
      // a react-select box shows its pick next to the (empty) search input
      const c = el.closest('[class*="control"], [class*="Control"]'); if (c) { const t = norm(c.textContent); value = /^(select|choose|search)/i.test(t) ? '' : t; }
    }
    e.fields[NAMES[m[1]]] = { ref: mkref(el), type, value, vis: vis(el), disabled: !!el.disabled, max: el.maxLength > 0 ? el.maxLength : 0 };
  }
  for (const e of Object.values(entries)) {
    const first = document.querySelector('[data-goat-ref="' + Object.values(e.fields)[0].ref + '"]');
    let node = first ? first.parentElement : null;
    for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
      const others = Array.from(node.querySelectorAll('input, select')).some(x => { const mm = /^(company-name|title)-(\d+)$/.exec(x.id || ''); return mm && parseInt(mm[2], 10) !== e.idx; });
      if (others) break;
      const del = Array.from(node.querySelectorAll('button')).find(b => /remove (employment|entry|position)|^remove$|^delete$/i.test(norm(b.getAttribute('aria-label') || b.textContent)));
      if (del) { e.delete = mkref(del); break; }
    }
  }
  // The employment section's Add button: the "Add another" that FOLLOWS the last employment block (Education has
  // its own "Add another"; Tenstorrent's got five blank school entries from it, 2026-10-18), or one whose own text
  // or nearest preceding heading names employment / experience when no block exists yet.
  const sections = [];
  const companyInputs = Array.from(document.querySelectorAll('input[id^="company-name-"]'));
  const lastBlock = companyInputs.length ? companyInputs[companyInputs.length - 1] : null;
  const headingBefore = (b) => { let n = b; while (n) { let p = n.previousElementSibling; while (p) { const h = p.matches('h1,h2,h3,h4,legend,[role=heading]') ? p : p.querySelector('h1,h2,h3,h4,legend,[role=heading]'); if (h) return norm(h.textContent); p = p.previousElementSibling; } n = n.parentElement; } return ''; };
  for (const b of document.querySelectorAll('button, a[role="button"]')) {
    const t = norm(b.textContent) || norm(b.getAttribute('aria-label') || '');
    if (!vis(b) || !/^add (another|employment|experience|position|job|work)( (employment|experience|position|job))?$/i.test(t)) continue;
    let ok = /employment|experience|position|job|work/i.test(t);
    if (!ok && lastBlock && (lastBlock.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)) {
      // the first "Add another" after the last employment block, with no other block's control in between
      const between = Array.from(document.querySelectorAll('input, select, textarea, button')).filter(x => (lastBlock.compareDocumentPosition(x) & Node.DOCUMENT_POSITION_FOLLOWING) && (x.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING));
      ok = !between.some(x => /^(school|degree|discipline|education|start-date|end-date)-?/i.test(x.id || '') && !/^(start|end)-date-(month|year)-\d+$/.test(x.id || ''));
    }
    if (!ok && !lastBlock) ok = /employment|experience|work history/i.test(headingBefore(b));
    if (ok) sections.push({ heading: 'Work Experience', ref: mkref(b), label: t });
  }
  return { sections, entries: Object.values(entries), has_skills: false, text_head: '' };
}
""".replace("%NAMES%", str(_NAMES).replace("'", '"'))

_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


class GreenhouseSections(WorkdaySections):
    REBUILD_UNTOUCHED = False

    async def scan(self) -> dict:
        try:
            d = await self.ex.eval(JS_GH_SECTIONS)
        except Exception as e:
            return {"sections": [], "entries": [], "error": str(e)}
        return d if isinstance(d, dict) else {"sections": [], "entries": []}

    async def _set_month_year(self, entry: dict, which: str, value: str, n: int) -> bool:
        """Greenhouse dates: a month picker (react-select or <select>) plus a four-digit year box."""
        parts = fill.parse_date(value)
        mon, yr = parts.get("month"), parts.get("year")
        combo, year = entry["fields"].get(f"{which}MonthCombo"), entry["fields"].get(f"{which}Year")
        if not combo or not year or combo.get("disabled"):
            return False
        ok = True
        name = _MONTHS[int(mon) - 1] if mon else ""
        if name and answers.norm(combo["value"]) != answers.norm(name):
            if combo["type"] == "select":
                r = (await fill.fill_batch(self.ex, [{"ref": combo["ref"], "value": name}]))[0]
            else:
                r = await fill.choose(self.ex, combo["ref"], [name, name[:3], mon])
            if r.ok:
                self._record(f"employment-{entry['idx']} {which} month", r.readback or name, f"experience.work.{n}.{which}", combo["ref"])
            else:
                ok = False
                self.report.failed.append(f"employment-{entry['idx']} {which} month: {r.error}")
        if yr and year["value"].strip() != yr:
            r = (await fill.fill_batch(self.ex, [{"ref": year["ref"], "value": yr}]))[0]
            if r.ok:
                self._record(f"employment-{entry['idx']} {which} year", r.readback, f"experience.work.{n}.{which}", year["ref"])
            else:
                ok = False
                self.report.failed.append(f"employment-{entry['idx']} {which} year: {r.error}")
        return ok

    async def fill_work_entry(self, entry: dict, ent: dict) -> bool:
        n = ent["n"]
        tag = f'{entry["kind"]}-{entry["idx"]}'
        self.changed = False
        ok_all = True
        for fname, key in (("jobTitle", "title"), ("companyName", "company")):
            f = entry["fields"].get(fname)
            if f and ent[key] and answers.norm(f["value"]) != answers.norm(ent[key]):
                self.changed = True
                if not await self._set(f["ref"], ent[key], f"{tag} {key}", f"experience.work.{n}.{key}"):
                    ok_all = False
                    self.report.failed.append(f"{tag} {key}")
        cur = entry["fields"].get("currentlyWorkHere")
        if cur and (cur["value"] == "checked") != bool(ent["current"]):
            self.changed = True
            if not await self._set(cur["ref"], bool(ent["current"]), f"{tag} current role", f"experience.work.{n}.current"):
                ok_all = False
                self.report.failed.append(f"{tag} current role")
            await asyncio.sleep(0.4)
        info = await self.scan()
        entry = next((e for e in info["entries"] if f'{e["kind"]}-{e["idx"]}' == tag), entry)
        for which, key in (("start", "start"), ("end", "end")):
            val = ent[key]
            if not val or (which == "end" and ent["current"]):
                continue
            combo, year = entry["fields"].get(f"{which}MonthCombo"), entry["fields"].get(f"{which}Year")
            parts = fill.parse_date(val)
            if combo and year and parts.get("month") and parts.get("year"):
                want_m = _MONTHS[int(parts["month"]) - 1]
                if answers.norm(combo["value"]) == answers.norm(want_m) and year["value"].strip() == parts["year"]:
                    continue
            self.changed = True
            if not await self._set_month_year(entry, which, val, n):
                ok_all = False
                if not combo or not year:
                    self.report.failed.append(f"{tag} {which} date: no month/year boxes")
        return ok_all

    async def run(self):
        await self.fill_work()
        return self.report


def is_employment_page(snap) -> bool:
    return any(re.match(r"^(company name|employer)\*?$", (i.get("label") or "").strip(), re.I) for i in snap.items)


def review_gaps_from_snapshot(snap, company: str = "") -> list[str]:
    """Every stored work entry's company must be the value of some company box on the page."""
    from .workday_sections import _same_company, stored_work_entries
    boxes = [str(i.get("value") or i.get("shown") or "") for i in snap.items if re.match(r"^(company name|employer)\*?$", (i.get("label") or "").strip(), re.I)]
    if not boxes:
        return []
    return [f'work entry {e["n"]} ({e["company"]})' for e in stored_work_entries(company)
            if not any(_same_company(b, e["company"]) for b in boxes)]
