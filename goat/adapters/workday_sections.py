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
    e.fields[m[3]] = { ref: mkref(el), type, value: type === 'checkbox' ? (el.checked ? 'checked' : '') : (el.value || ''), vis: vis(el), max: el.maxLength > 0 ? el.maxLength : 0 };
  }
  // each block's own Delete button: the nearest ancestor of its first field that holds one and no other block's fields
  for (const e of Object.values(entries)) {
    const first = document.querySelector('[data-goat-ref="' + Object.values(e.fields)[0].ref + '"]');
    let node = first ? first.parentElement : null;
    for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
      const others = Array.from(node.querySelectorAll('input, textarea')).some(x => { const m = /^([A-Za-z]+)-(\d+)--/.exec(x.id || ''); return m && (m[1] !== e.kind || parseInt(m[2], 10) !== e.idx); });
      if (others) break;
      const del = Array.from(node.querySelectorAll('button')).find(b => /^delete$/i.test(norm(b.textContent)) || /^delete$/i.test(norm(b.getAttribute('aria-label') || '')));
      if (del) { e.delete = mkref(del); break; }
    }
  }
  const text = norm(document.body.innerText || '').slice(0, 20000);
  return { sections, entries: Object.values(entries), has_skills: /\bSkills\b/.test(text), text_head: text.slice(0, 400) };
}
"""

WORK_KINDS = ("workExperience", "workexperience", "experience", "employment")
SITE_KINDS = ("websites", "website", "webAddress", "webAddresses")


@dataclass
class SectionReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    present: list[str] = field(default_factory=list)   # already complete, nothing touched

    def summary(self) -> dict:
        return {"added": self.added, "updated": self.updated, "present": self.present, "failed": self.failed, "skipped": self.skipped}


def fit_bullets(text: str, cap: int) -> str:
    """Keep whole bullets (lines) that fit a character cap; if even the first does not, cut it at a sentence end."""
    lines, out = text.split("\n"), []
    for ln in lines:
        if len("\n".join(out + [ln])) <= cap:
            out.append(ln)
        else:
            break
    if out:
        return "\n".join(out)
    first = lines[0][:cap]
    cut = max(first.rfind(". "), first.rfind("; "), first.rfind(", "))
    return (first[:cut + 1] if cut > cap // 2 else first).rstrip()


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
        self.changed = False

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

    async def _click_add(self, section_heading: re.Pattern, seen: set[str], kinds: tuple[str, ...]) -> dict | None:
        """Click the section's Add / Add Another button and return the entry OF THAT KIND that appeared
        (Lowe's renders the Education and Languages blocks lazily: they must not be taken for a work entry)."""
        info = await self.scan()
        btn = next((s for s in info["sections"] if section_heading.search(s["heading"])), None)
        if not btn:
            return None
        await self.ex.click_ref(btn["ref"])
        for _ in range(8):
            await asyncio.sleep(0.5)
            info = await self.scan()
            new = [e for e in info["entries"] if f'{e["kind"]}-{e["idx"]}' not in seen and e["fields"] and e["kind"] in kinds]
            if new:
                return new[-1]
        return None

    async def _delete_entry(self, entry: dict) -> bool:
        """Press the Delete button of a blank block (some tenants pre-render one per section)."""
        if not entry.get("delete"):
            return False
        try:
            await self.ex.click_ref(entry["delete"])
        except Exception:
            return False
        await asyncio.sleep(0.6)
        return True

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
        self.changed = bool(plan)
        for ref, val, label, key in plan:
            if not await self._set(ref, val, label, key):
                ok_all = False
                self.report.failed.append(label)
        # "I currently work here" first: the To date exists only while it is unchecked.
        cur = _field(entry, "currentlyWorkHere", "current")
        if cur:
            want = bool(ent["current"])
            if (cur["value"] == "checked") != want:
                self.changed = True
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
            self.changed = True
            r = await fill.set_date(self.ex, mref, val)
            if r.ok:
                self._record(f"{tag} {which} date", r.readback, f"experience.work.{n}.{key}", mref)
            else:
                ok_all = False
                self.report.failed.append(f"{tag} {which} date: {r.error}")
        desc = _field(entry, "roleDescription", "description")
        if desc and ent["description"]:
            want = bullets_to_lines(ent["description"])
            if desc.get("max") and len(want) > desc["max"]:
                want = fit_bullets(want, desc["max"])
                self.report.skipped.append(f"{tag} description trimmed to the box's {desc['max']}-character cap")
            if answers.norm(desc["value"]) != answers.norm(want):
                self.changed = True
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
                if self.changed or not ok:
                    self.report.updated.append(f'{ent["company"]}{"" if ok else " (partial)"}')
                else:
                    self.report.present.append(ent["company"])
                continue
            # a blank block (pre-rendered by the tenant, or left by an earlier Add) is filled before another Add
            new = next((e for e in existing if f'{e["kind"]}-{e["idx"]}' not in matched and _is_blank_work(e)), None)
            if new is None:
                new = await self._click_add(work_heading, seen, WORK_KINDS)
            if not new:
                self.report.failed.append(f'{ent["company"]}: Add button not found or no new entry appeared')
                continue
            seen.add(f'{new["kind"]}-{new["idx"]}')
            matched.add(f'{new["kind"]}-{new["idx"]}')
            ok = await self.fill_work_entry(new, ent)
            self.report.added.append(f'{ent["company"]}{"" if ok else " (partial)"}')
        # Leftover blank blocks go; entries holding a company that is not on his résumé are reported, never deleted.
        info = await self.scan()
        for e in [x for x in info["entries"] if x["kind"] in WORK_KINDS]:
            tag = f'{e["kind"]}-{e["idx"]}'
            if tag in matched:
                continue
            if _is_blank_work(e):
                if await self._delete_entry(e):
                    self.report.updated.append(f"removed blank entry {tag}")
                else:
                    self.report.skipped.append(f"blank entry {tag} has no Delete button")
                continue
            c = _field(e, "companyName", "company")
            if c and any(_same_company(c["value"], s["company"]) for s in stored):
                # a second copy of one of his own entries (an earlier pass added it twice): ours to remove
                if await self._delete_entry(e):
                    self.report.updated.append(f'removed duplicate entry {tag} ({c["value"][:30]})')
                    continue
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
                target = await self._click_add(site_heading, seen, SITE_KINDS)
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

    # ---- Skills: Workday's "Type to Add Skills" multi-select ----------------------------------------------
    # Verified on ICF (2026-10-18): the search runs on Enter and lists rows with checkboxes; clicking the
    # matching promptOption row selects exactly that skill. Once a chip exists the search box hides inside
    # the chip list and keyboard focus lands on a chip (an Enter or Ctrl+A there selects whole lists), so the
    # box is focused explicitly and re-checked before every keystroke.
    JS_SKILLS = r"""() => {
      const q = (s) => Array.from(document.querySelectorAll(s));
      let inp = document.getElementById('skills--skills') || q('input[id$="--skills"], input[id*="skill" i]')[0] || null;
      if (!inp) {
        const h = q('h2,h3,h4,[role=heading],label').find(e => /^(skills|type to add skills)$/i.test(e.textContent.trim()));
        let box = h; for (let i = 0; i < 6 && box && !box.querySelector('input'); i++) box = box.parentElement;
        inp = box ? box.querySelector('input') : null;
      }
      if (!inp) return { found: false };
      const cont = inp.closest('[data-automation-id="multiSelectContainer"], [data-automation-id^="formField"]') || inp.parentElement;
      const r = cont.getBoundingClientRect();
      const icon = cont.querySelector('[data-automation-id="promptIcon"]');
      const ir = icon ? icon.getBoundingClientRect() : null;
      const chips = q('[data-automation-id="selectedItem"]').filter(e => cont.contains(e)).map(e => e.textContent.trim());
      const seen = new Set();
      const opts = q('[data-automation-id="promptOption"]').filter(e => e.getBoundingClientRect().height > 0)
        .map(e => e.textContent.trim()).filter(t => t && !seen.has(t) && seen.add(t));
      const txt = (document.body.innerText || '');
      const m = /Search Results\s*\((\d+)\)/i.exec(txt);
      return { found: true, active: document.activeElement === inp, value: inp.value || '', chips, opts,
               results: m ? parseInt(m[1], 10) : -1,
               box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
               icon: ir ? [Math.round(ir.x + ir.width / 2), Math.round(ir.y + ir.height / 2)] : null };
    }"""
    JS_FOCUS_SKILLS = r"""() => { const inp = document.getElementById('skills--skills') || document.querySelector('input[id$="--skills"], input[id*="skill" i]');
      if (!inp) return false; inp.scrollIntoView({block: 'center'}); inp.focus(); return document.activeElement === inp; }"""
    JS_CLICK_OPTION = r"""(text) => { const o = Array.from(document.querySelectorAll('[data-automation-id="promptOption"]')).find(e => e.textContent.trim() === text && e.getBoundingClientRect().height > 0);
      if (!o) return false; const cb = o.closest('[role="option"], li, label') && o.closest('[role="option"], li, label').querySelector('input[type="checkbox"], [role="checkbox"]'); (cb || o).click(); return true; }"""

    async def _skills_state(self) -> dict:
        try:
            d = await self.ex.eval(self.JS_SKILLS)
        except Exception:
            return {"found": False}
        return d if isinstance(d, dict) else {"found": False}

    async def _focus_skills_box(self, prefer_click: bool = True) -> bool:
        st = await self._skills_state()
        if not st.get("found"):
            return False
        x, y, w, h = st["box"]

        async def by_click() -> bool:
            # the free space right of the chips, left of the prompt icon: what a person would click
            for px, py in ((x + w - 50, y + h - 20), (x + w - 50, y + 20), (x + 12, y + h - 20)):
                try:
                    await self.ex.click_xy(px, py)
                except Exception:
                    continue
                await asyncio.sleep(0.4)
                if (await self._skills_state()).get("active"):
                    return True
            return False

        async def by_js() -> bool:
            ok = await self.ex.eval(self.JS_FOCUS_SKILLS)
            await asyncio.sleep(0.3)
            return bool(ok and (await self._skills_state()).get("active"))

        for step in ((by_click, by_js) if prefer_click else (by_js, by_click)):
            if await step():
                return True
        if st.get("icon"):
            try:
                await self.ex.click_xy(*st["icon"])
                await asyncio.sleep(0.6)
                await self.ex.page.keyboard.press("Escape")
                await asyncio.sleep(0.3)
            except Exception:
                pass
            if await by_js():
                return True
        return bool((await self._skills_state()).get("active"))

    async def _search_skill(self, skill: str) -> list[str]:
        """Type the skill into the focused box, press Enter, wait for the result list to (re)appear."""
        st = await self._skills_state()
        if st.get("value"):
            # never Ctrl+A here (it selects the whole result list when focus slips to the listbox): End + Backspaces
            await self.ex.page.keyboard.press("End")
            for _ in range(len(st["value"]) + 2):
                await self.ex.page.keyboard.press("Backspace")
            await asyncio.sleep(0.3)
            st = await self._skills_state()
            if st.get("value"):
                self.report.failed.append(f"skill {skill}: could not clear the old text {st['value']!r}")
                return []
        chips = set(st.get("chips") or [])
        before = set(o for o in (st.get("opts") or []) if o not in chips)
        typed = False
        for attempt in range(2):
            await self.ex.type_text(skill, real_keys=True, delay_ms=25)
            await asyncio.sleep(0.25)
            st = await self._skills_state()
            if st.get("active") and answers.norm(st.get("value", "")) == answers.norm(skill):
                typed = True
                break
            # the keystrokes went elsewhere (a re-rendered box, an open result list): close, refocus the other way, retry
            await self.ex.page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
            if not await self._focus_skills_box(prefer_click=attempt == 1):
                break
        if not typed:
            self.report.failed.append(f"skill {skill}: the box reads {st.get('value')!r} (active={st.get('active')}) after typing")
            return []
        await self.ex.page.keyboard.press("Enter")
        for _ in range(15):
            await asyncio.sleep(0.4)
            st = await self._skills_state()
            opts = [o for o in (st.get("opts") or []) if o not in chips]
            fresh = [o for o in opts if o not in before]
            if st.get("results", -1) >= 0 and (fresh or st.get("results") == 0):
                await asyncio.sleep(0.3)
                st = await self._skills_state()
                return [o for o in (st.get("opts") or []) if o not in chips and o not in before]
        return []

    async def fill_skills(self) -> None:
        a = answers.get("experience.skills_list", self.company, "workday")
        if not a or not a.has_value:
            return
        skills = [s.strip() for s in str(a.answer).split(";") if s.strip()]
        st = await self._skills_state()
        if not st.get("found"):
            return
        failures = 0
        for skill in skills:
            chips = (await self._skills_state()).get("chips") or []
            if any(fill._chip_matches(c, [skill]) for c in chips):
                self.report.present.append(f"skill {skill}")
                continue
            if failures >= 2:
                self.report.skipped.append(f"skill {skill}: stopped after two picker failures")
                continue
            if not await self._focus_skills_box():
                failures += 1
                self.report.failed.append(f"skill {skill}: could not focus the search box")
                continue
            opts = await self._search_skill(skill)
            pick = pick_skill(skill, opts)
            if not pick:
                # a lone exact result is selected by Enter itself (FastAPI on ICF): the chip is already there
                now = (await self._skills_state()).get("chips") or []
                gained = [c for c in now if c not in chips]
                if len(gained) == 1 and fill._chip_matches(gained[0], [skill]):
                    self._record(f"skill {skill}", gained[0], "experience.skills_list", "skills--skills")
                    self.report.added.append(f"skill {gained[0]}")
                    await self.ex.page.keyboard.press("Escape")
                    continue
                for c in gained:
                    await self.remove_chip(c)
                if opts:
                    self.report.skipped.append(f"skill {skill}: not offered (closest: {opts[:4]})")
                else:
                    failures += 1
                    self.report.failed.append(f"skill {skill}: the search returned nothing")
                await self.ex.page.keyboard.press("Escape")
                continue
            await self.ex.eval(self.JS_CLICK_OPTION, pick)
            await asyncio.sleep(0.8)
            after = (await self._skills_state()).get("chips") or []
            new = [c for c in after if c not in chips]
            await self.ex.page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
            if len(new) == 1 and fill._chip_matches(new[0], [pick, skill]):
                self._record(f"skill {skill}", new[0], "experience.skills_list", "skills--skills")
                self.report.added.append(f"skill {new[0]}")
            else:
                failures += 1
                # anything that is not the one wanted chip comes off again
                for c in new:
                    if not fill._chip_matches(c, [pick, skill]):
                        await self.remove_chip(c)
                self.report.failed.append(f"skill {skill}: clicking {pick!r} produced {new}")

    async def remove_chip(self, text: str) -> bool:
        try:
            return bool(await self.ex.eval(
                """(t) => { const c = Array.from(document.querySelectorAll('[data-automation-id="selectedItem"]')).find(e => e.textContent.trim() === t);
                   const x = c && c.querySelector('[data-automation-id="DELETE_charm"]'); if (!x) return false; x.click(); return true; }""", text))
        except Exception:
            return False

    async def run(self) -> SectionReport:
        await self.fill_work()
        await self.fill_websites()
        await self.fill_skills()
        return self.report


def pick_skill(skill: str, options: list[str]) -> str | None:
    """Exact wording first, then 'Skill (qualifier)', then '... (SKILL)' acronyms, then an unambiguous prefix.
    'SQL' must take 'Structured Query Language (SQL)', never 'SQLCLR'."""
    w = answers.norm(skill)
    if not w:
        return None
    for o in options:
        if answers.norm(o) == w:
            return o
    for o in options:
        # only qualifiers that cannot change the meaning: 'ReAct Framework' is not React, so 'framework' is not one
        if re.fullmatch(re.escape(skill.strip().lower()) + r"\s*(\(.*\)|programming language|software)", o.strip().lower()):
            return o
    for o in options:
        if re.search(r"\(\s*" + re.escape(skill.strip()) + r"\s*\)", o, re.I):
            return o
    # 'React' -> 'React.js', 'Node' -> 'Node.js': the same thing once punctuation and a trailing js are squashed
    sq = re.sub(r"[^a-z0-9]", "", skill.lower())
    for o in options:
        so = re.sub(r"[^a-z0-9]", "", o.lower())
        if so in (sq, sq + "js") or (sq.endswith("js") and so == sq[:-2]):
            return o
    starts = [o for o in options if answers.norm(o).startswith(w + " ")]
    if len(starts) == 1:
        return starts[0]
    return None


def _is_blank_work(entry: dict) -> bool:
    c = _field(entry, "companyName", "company", "employer")
    t = _field(entry, "jobTitle", "title")
    return bool(c or t) and not ((c and c["value"].strip()) or (t and t["value"].strip()))


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
