"""Deterministic Simplify-style filler for Workday's "My Experience" page.

Workday renders every repeating section with stable element ids: `workExperience-4--jobTitle`,
`workExperience-4--companyName`, `--location`, `--currentlyWorkHere`, `--startDate-dateSectionMonth-input`,
`--endDate-dateSectionYear-input`, `--roleDescription`, and the same `<kind>-<n>--<field>` shape for
websites, languages and education. Each section has one `data-automation-id="add-button"` ("Add", then
"Add Another"). The model used to walk these by hand and dropped entries (Nissan: none; AtkinsRéalis: two of
four); this module adds every stored work entry and website, with the full résumé bullets, and reports what
it could not set so the model only has to finish the rest (Education's school prompt, Skills)."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from .. import answers, records
from ..browser import fill

# One JS pass: sections (heading -> add button ref), entries (kind, index -> field refs/values).
JS_SECTIONS = r"""
() => {
  let counter = 0;
  for (const el of document.querySelectorAll('[data-goat-ref]')) {
    const n = parseInt((el.getAttribute('data-goat-ref') || 'e0').slice(1), 10); if (n > counter) counter = n;
  }
  const mkref = (el) => { let r = el.getAttribute('data-goat-ref'); if (!r) { r = 'e' + (++counter); el.setAttribute('data-goat-ref', r); } return r; };
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const sections = [];
  let heading = '';
  for (const el of document.querySelectorAll('h2, h3, h4, [role="heading"], button[data-automation-id="add-button"]')) {
    if (el.tagName === 'BUTTON') { if (vis(el)) sections.push({ heading, ref: mkref(el), label: norm(el.textContent) }); }
    else heading = norm(el.textContent);
  }
  const entries = {};
  for (const el of document.querySelectorAll('input, textarea')) {
    const m = /^([A-Za-z]+)-(\d+)--(.+)$/.exec(el.id || '');
    if (!m) continue;
    const key = m[1] + '-' + m[2];
    const e = entries[key] || (entries[key] = { kind: m[1], idx: parseInt(m[2], 10), fields: {} });
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    e.fields[m[3]] = { ref: mkref(el), type, value: type === 'checkbox' ? (el.checked ? 'checked' : '') : (el.value || ''), vis: vis(el) };
  }
  const text = norm(document.body.innerText || '').slice(0, 20000);
  return { sections, entries: Object.values(entries), has_skills: /\bSkills\b/.test(text), text_head: text.slice(0, 400) };
}
"""

WORK_KINDS = ("workExperience", "workexperience", "experience")
SITE_KINDS = ("websites", "website", "webAddress", "webAddresses")


@dataclass
class SectionReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {"added": self.added, "updated": self.updated, "failed": self.failed, "skipped": self.skipped}


def stored_work_entries(company: str = "", portal: str = "workday") -> list[dict]:
    """experience.work.1..N from the questionnaire, in order; stops at the first missing company."""
    out = []
    for n in range(1, 20):
        c = answers.get(f"experience.work.{n}.company", company, portal)
        if not c or not c.has_value:
            break
        ent = {"n": n, "company": str(c.answer).strip()}
        for f in ("title", "location", "start", "end", "current", "description"):
            a = answers.get(f"experience.work.{n}.{f}", company, portal)
            ent[f] = str(a.answer).strip() if a and a.has_value and a.answer is not None else ""
        ent["current"] = ent["current"].lower() in ("yes", "true", "y", "1")
        out.append(ent)
    return out


def stored_websites(company: str = "", portal: str = "workday") -> list[str]:
    a = answers.get("experience.websites", company, portal)
    if not a or not a.has_value:
        return []
    return [w.strip() for w in re.split(r"[;\n]", str(a.answer)) if w.strip().startswith("http")]


def bullets_to_lines(desc: str) -> str:
    """The store flattens whitespace: put every bullet back on its own line."""
    d = desc.replace("\\n", "\n")
    if "•" in d:
        d = re.sub(r"\s*•\s*", "\n• ", d).strip()
    return d


def _company_key(name: str) -> str:
    """'Mai Shan Yun (WaiterDock)' -> 'mai shan yun'; 'Rathinam Group, Texas A&M ...' -> 'rathinam group'."""
    base = re.split(r"\s*[(,]", name, 1)[0]
    return answers.norm(base)


def _same_company(shown: str, stored: str) -> bool:
    a, b = answers.norm(shown), answers.norm(stored)
    if not a or not b:
        return False
    return a == b or a.startswith(_company_key(stored)) or b.startswith(_company_key(shown))


def _field(entry: dict, *names: str) -> dict | None:
    for nm, f in entry["fields"].items():
        low = nm.lower()
        if any(low == n.lower() or low.startswith(n.lower()) for n in names):
            return f
    return None


def _date_month_ref(entry: dict, which: str) -> str | None:
    f = _field(entry, f"{which}Date-dateSectionMonth", f"{which}Date-month", f"{which}-dateSectionMonth")
    if f and f.get("vis"):
        return f["ref"]
    return None


class WorkdaySections:
    def __init__(self, ex, job: dict, page_no: int):
        self.ex, self.job, self.page_no = ex, job, page_no
        self.company = job.get("company", "")
        self.report = SectionReport()

    async def scan(self) -> dict:
        try:
            d = await self.ex.eval(JS_SECTIONS)
        except Exception as e:
            return {"sections": [], "entries": [], "error": str(e)}
        return d if isinstance(d, dict) else {"sections": [], "entries": []}

    def _record(self, label: str, value: str, key: str, ref: str) -> None:
        records.record_decision(self.job["id"], self.page_no, label, value, f"standing:{key}", ref)

    async def _set(self, ref: str, value, label: str, key: str) -> bool:
        out = await fill.fill_batch(self.ex, [{"ref": ref, "value": value}])
        ok = bool(out and out[0].ok)
        if ok:
            self._record(label, out[0].readback if isinstance(value, str) else str(value), key, ref)
        return ok

    async def _click_add(self, section_heading: re.Pattern, seen: set[str]) -> dict | None:
        """Click the section's Add / Add Another button and return the entry that appeared."""
        info = await self.scan()
        btn = next((s for s in info["sections"] if section_heading.search(s["heading"])), None)
        if not btn:
            return None
        await self.ex.click_ref(btn["ref"])
        for _ in range(8):
            await asyncio.sleep(0.5)
            info = await self.scan()
            new = [e for e in info["entries"] if f'{e["kind"]}-{e["idx"]}' not in seen and e["fields"]]
            if new:
                return new[-1]
        return None

    async def fill_work_entry(self, entry: dict, ent: dict) -> bool:
        n = ent["n"]
        tag = f'{entry["kind"]}-{entry["idx"]}'
        plan = []
        for fname, names, key in (("title", ("jobTitle", "title"), "title"), ("company", ("companyName", "company", "employer"), "company"),
                                  ("location", ("location",), "location")):
            f = _field(entry, *names)
            if f and ent[key] and answers.norm(f["value"]) != answers.norm(ent[key]):
                plan.append((f["ref"], ent[key], f"{tag} {fname}", f"experience.work.{n}.{key}"))
        ok_all = True
        for ref, val, label, key in plan:
            if not await self._set(ref, val, label, key):
                ok_all = False
                self.report.failed.append(label)
        # "I currently work here" first: the To date exists only while it is unchecked.
        cur = _field(entry, "currentlyWorkHere", "current")
        if cur:
            want = bool(ent["current"])
            if (cur["value"] == "checked") != want:
                if not await self._set(cur["ref"], want, f"{tag} currently work here", f"experience.work.{n}.current"):
                    ok_all = False
                    self.report.failed.append(f"{tag} currently work here")
                await asyncio.sleep(0.6)
        # Dates: re-scan, the To widget may have appeared or gone.
        info = await self.scan()
        entry = next((e for e in info["entries"] if f'{e["kind"]}-{e["idx"]}' == tag), entry)
        for which, key in (("start", "start"), ("end", "end")):
            val = ent[key]
            if not val or (which == "end" and ent["current"]):
                continue
            mref = _date_month_ref(entry, which)
            if not mref:
                if which == "start":
                    self.report.failed.append(f"{tag} start date: no date widget found")
                    ok_all = False
                continue
            mon = _field(entry, f"{which}Date-dateSectionMonth")
            yr = _field(entry, f"{which}Date-dateSectionYear")
            parts = fill.parse_date(val)
            if mon and yr and mon["value"].zfill(2) == parts.get("month", "") and yr["value"] == parts.get("year", ""):
                continue
            r = await fill.set_date(self.ex, mref, val)
            if r.ok:
                self._record(f"{tag} {which} date", r.readback, f"experience.work.{n}.{key}", mref)
            else:
                ok_all = False
                self.report.failed.append(f"{tag} {which} date: {r.error}")
        desc = _field(entry, "roleDescription", "description")
        if desc and ent["description"]:
            want = bullets_to_lines(ent["description"])
            if answers.norm(desc["value"]) != answers.norm(want):
                if not await self._set(desc["ref"], want, f"{tag} role description", f"experience.work.{n}.description"):
                    ok_all = False
                    self.report.failed.append(f"{tag} role description")
        return ok_all

    async def fill_work(self) -> None:
        stored = stored_work_entries(self.company)
        if not stored:
            return
        info = await self.scan()
        work_heading = re.compile(r"^work (experience|history)", re.I)
        if not any(work_heading.search(s["heading"]) for s in info["sections"]) and not any(e["kind"] in WORK_KINDS for e in info["entries"]):
            self.report.skipped.append("no Work Experience section on this page")
            return
        existing = [e for e in info["entries"] if e["kind"] in WORK_KINDS]
        seen = {f'{e["kind"]}-{e["idx"]}' for e in info["entries"]}
        matched: set[str] = set()
        for ent in stored:
            comp = _field_value(existing, ent["company"])
            if comp is not None:
                tag = f'{comp["kind"]}-{comp["idx"]}'
                matched.add(tag)
                ok = await self.fill_work_entry(comp, ent)
                self.report.updated.append(f'{ent["company"]}{"" if ok else " (partial)"}')
                continue
            new = await self._click_add(work_heading, seen)
            if not new:
                self.report.failed.append(f'{ent["company"]}: Add button not found or no new entry appeared')
                continue
            seen.add(f'{new["kind"]}-{new["idx"]}')
            matched.add(f'{new["kind"]}-{new["idx"]}')
            ok = await self.fill_work_entry(new, ent)
            self.report.added.append(f'{ent["company"]}{"" if ok else " (partial)"}')
        # Entries holding a company that is not on his résumé are reported, never deleted (a human decision).
        for e in existing:
            tag = f'{e["kind"]}-{e["idx"]}'
            if tag not in matched:
                c = _field(e, "companyName", "company")
                self.report.skipped.append(f'extra entry {tag}: {c["value"][:40] if c else "?"}')

    async def fill_websites(self) -> None:
        sites = stored_websites(self.company)
        if not sites:
            return
        info = await self.scan()
        site_heading = re.compile(r"^(websites?|web ?address|social|links)", re.I)
        if not any(site_heading.search(s["heading"]) for s in info["sections"]) and not any(e["kind"] in SITE_KINDS for e in info["entries"]):
            return
        existing = [e for e in info["entries"] if e["kind"] in SITE_KINDS]
        have = {answers.norm(_field(e, "url", "website", "address")["value"]) for e in existing if _field(e, "url", "website", "address")}
        seen = {f'{e["kind"]}-{e["idx"]}' for e in info["entries"]}
        for i, url in enumerate(sites, 1):
            if answers.norm(url) in have or any(h and (h in answers.norm(url) or answers.norm(url) in h) for h in have):
                continue
            # an empty URL box left by a bare Add is reused before another Add
            target = None
            for e in existing:
                f = _field(e, "url", "website", "address")
                if f and not f["value"]:
                    target = e
                    existing = [x for x in existing if x is not e]
                    break
            if target is None:
                target = await self._click_add(site_heading, seen)
                if not target:
                    self.report.failed.append(f"website {url}: Add button not found")
                    continue
                seen.add(f'{target["kind"]}-{target["idx"]}')
            f = _field(target, "url", "website", "address")
            if not f:
                self.report.failed.append(f"website {url}: no URL box in the new entry")
                continue
            if await self._set(f["ref"], url, f"website {i}", "experience.websites"):
                self.report.added.append(url)
            else:
                self.report.failed.append(f"website {url}")

    async def run(self) -> SectionReport:
        await self.fill_work()
        await self.fill_websites()
        return self.report


def _field_value(entries: list[dict], company: str) -> dict | None:
    for e in entries:
        c = _field(e, "companyName", "company", "employer")
        if c and _same_company(c["value"], company):
            return e
    return None


def is_experience_page(info_text: str, headings: list[str]) -> bool:
    hay = " ".join(headings) + " " + info_text
    return bool(re.search(r"\b(My Experience|Work Experience|Work History)\b", hay, re.I))


def review_gaps(page_text: str, company: str = "") -> list[str]:
    """On a Workday Review page: every stored work entry's company and every stored website must appear."""
    gaps = []
    text = answers.norm(page_text)
    for ent in stored_work_entries(company):
        if _company_key(ent["company"]) not in text:
            gaps.append(f'work entry {ent["n"]} ({ent["company"]})')
    if re.search(r"\bWebsites\b", page_text):
        for url in stored_websites(company):
            host_path = re.sub(r"^https?://(www\.)?", "", url).rstrip("/").lower()
            if host_path not in page_text.lower():
                gaps.append(f"website {url}")
    return gaps
