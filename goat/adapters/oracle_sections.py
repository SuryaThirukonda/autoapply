"""Oracle Recruiting Cloud: the Experience and Education entries, written deterministically from his stored entries.

Oracle keeps one candidate profile per tenant and shows each saved entry either as a tile in an 'Experience' /
'Education' block ('Founding Software Engineer (Part-time)' over 'Revio 02/2026', with Edit and Delete icons; Uber,
GM Financial) or as a block on a 'Work and Education History' timeline that shows Edit / Remove on hover (BNY). Its
résumé import, and earlier runs, left entries that are not his: skill lists as Employer Name ('Quantitative Systems,
FastAPI, QuestDB, Docker'), 'Unnamed Job Title' over his Southwest dates, an 'Engineering Honors' education entry and a
degree tile with no school or degree (GM Financial, BNY, Citizens, DP World, 2026-10-21/22). The model edited them by
hand, mostly failed, and parked ten applications.

Here every entry is matched to a stored one (experience.work.N by employer, then job title, then dates; education.* by
major, or school with a degree): an entry that is none of his is deleted (Delete acts at once, no confirmation), his
own entries are opened and every field that differs is set, missing ones are added, and each form is saved. An entry
form's inputs carry stable id prefixes: employerName-N, jobTitle-N, month-/year-startDate-N, month-/year-endDate-N,
af-checkbox-currentJobFlag-N (not on every tenant), employerCity-N, countryCode-N, responsibilities-N; contentItemId-N
(Degree), major-N, educationalEstablishment(Id)-N (School), af-checkbox-graduatedFlag-N. Month, Year, Degree, School and
Country are cx-select pickers whose list (#<id>-listbox) is a role=grid of gridcells; the form's footer holds .save-btn
('Save', or 'Add experience' on a new entry) and .cancel-btn."""
from __future__ import annotations

import asyncio
import json
import re

from .. import answers, records
from ..browser import fill
from .workday_sections import SectionReport, _same_company, bullets_to_lines, fit_bullets, stored_work_entries

WAIT_MS = 6000
DESC_CAP = 2000          # the Responsibilities box shows no maxlength; his longest description is about 600 characters
MAX_ACTIONS = 24         # opens, deletes and adds in one run: a page that keeps undoing them is reported, not fought
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
          "December")
TILE_DATES = re.compile(r"\s*\d{1,2}/\d{4}(\s*-\s*(\d{1,2}/\d{4}|present|current))?\s*$", re.I)
UNNAMED = re.compile(r"^\s*unnamed\b", re.I)
HIS_DEGREE = re.compile(r"bachelor|undergraduate|computer science|\bb\.?\s?s\b", re.I)
GENERIC_BACHELOR = re.compile(r"(bachelor|bachelors|bachelor s|bachelor degree|bachelors degree|bachelor s degree|"
                              r"baccalaureate|undergraduate|undergraduate degree)")
DESC_NAMES = ("responsibilities", "achievements", "jobDescription", "description")
SCHOOL_NAMES = ("educationalEstablishmentId", "educationalEstablishment")

# Experience / Education blocks on the page: tiles (Uber, GM Financial) or timeline entries (BNY), their add buttons,
# and whether an entry form is open.
JS_SCAN = r"""
() => {
  const vis = (e) => { if (!e) return false; const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const out = { blocks: [], form: Array.from(document.querySelectorAll('.profile-item-content--form, .standard-apply-flow-timeline-form')).some(vis) };
  Array.from(document.querySelectorAll('.apply-flow-block__form-list[aria-label]')).forEach((b, bidx) => {
    const raw = norm(b.getAttribute('aria-label'));
    const label = raw.replace(/\s*Required\s*$/i, '').trim();
    const timeline = !!b.querySelector('.beautiful-timeline, .timeline-container, .beautiful-timeline-item');
    // never 'Work Preferences' (GM Financial's questions block)
    const kindOf = /education/i.test(label) ? 'education' : (/experience|employment|work history/i.test(label) ? 'work' : 'other');
    const tiles = [];
    if (timeline) {
      b.querySelectorAll('.beautiful-timeline-item[data-profile-item-tile]').forEach((t, idx) => {
        const cls = String(t.className || '');
        const sub = t.querySelector('.beautiful-timeline-item__subtitle');
        tiles.push({ idx, kind: /--experien/i.test(cls) ? 'work' : (/--educ/i.test(cls) ? 'education' : ''),
          title: norm((t.querySelector('.beautiful-timeline-item__title') || {}).textContent), sub: sub ? norm(sub.textContent) : '',
          dates: norm((t.querySelector('.oj-helper-hidden-accessible') || {}).textContent),
          invalid: /invalid-item/.test(cls) || t.getAttribute('data-profile-item-invalid') === 'true', visible: vis(t) });
      });
    } else {
      b.querySelectorAll('article.apply-flow-profile-item-tile').forEach((t, idx) => {
        tiles.push({ idx, kind: kindOf, title: norm((t.querySelector('.apply-flow-profile-item-tile__summary-title') || {}).textContent),
          sub: norm((t.querySelector('.apply-flow-profile-item-tile__summary-subtitle') || {}).textContent), dates: '',
          invalid: /--invalid/.test(String(t.className || '')) || !!t.querySelector('[data-profile-item-invalid="true"]'), visible: vis(t) });
      });
    }
    const addWork = b.querySelector('button.timeline-work-add-button'), addEdu = b.querySelector('button.timeline-education-add-button');
    const addTile = b.querySelector('button.apply-flow-profile-item-tile__new-tile');
    const kind = timeline ? 'mixed' : kindOf;
    if (kind === 'other' || (!tiles.length && !addWork && !addEdu && !addTile)) return;
    out.blocks.push({ bidx, label, required: /Required\s*$/i.test(raw), visible: vis(b), layout: timeline ? 'timeline' : 'tiles', kind,
      add: { work: !!(addWork || (kind === 'work' && addTile)), education: !!(addEdu || (kind === 'education' && addTile)) }, tiles });
  });
  return JSON.stringify(out);
}
"""

# The open entry form: its inputs by id prefix (refs for the fill helpers), its Save / Cancel, its error rows.
JS_FORM = r"""
() => {
  const vis = (e) => { if (!e) return false; const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const f = Array.from(document.querySelectorAll('.profile-item-content--form, .standard-apply-flow-timeline-form')).find(vis);
  if (!f) return JSON.stringify({ open: false });
  let counter = 0;
  for (const el of document.querySelectorAll('[data-goat-ref]')) {
    const n = parseInt((el.getAttribute('data-goat-ref') || 'e0').slice(1), 10); if (n > counter) counter = n;
  }
  const mkref = (el) => { let r = el.getAttribute('data-goat-ref'); if (!r) { r = 'e' + (++counter); el.setAttribute('data-goat-ref', r); } return r; };
  const host = f.closest('.standard-apply-flow-profile-item') || f;
  const fields = {};
  for (const el of f.querySelectorAll('input, textarea')) {
    if (el.type === 'hidden' || /--helper/.test(String(el.className))) continue;
    const m = /^(?:af-checkbox-)?(.+?)-\d+$/.exec(el.id || '');
    if (!m || fields[m[1]]) continue;
    const row = el.closest('.input-row');
    const lab = el.id ? document.querySelector('label[for="' + el.id + '"]') : null;
    fields[m[1]] = { ref: mkref(el), id: el.id, tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || el.tagName).toLowerCase(),
      value: el.value || '', checked: !!el.checked, combo: el.getAttribute('role') === 'combobox', vis: vis(el) || vis(lab), disabled: !!el.disabled,
      required: el.getAttribute('aria-required') === 'true' || !!(row && row.querySelector('.input-row__label--required')),
      invalid: el.getAttribute('aria-invalid') === 'true' || !!(row && row.classList.contains('input-row--invalid')),
      max: el.maxLength > 0 ? el.maxLength : 0, label: norm(lab ? lab.innerText : (el.getAttribute('aria-label') || '')).slice(0, 60) };
  }
  // single-select pills (BNY's Degree: a role=radiogroup of role=radio buttons, no input)
  const pills = {};
  for (const ul of f.querySelectorAll('ul.cx-select-pills-container, [role="radiogroup"]')) {
    const key = norm(ul.getAttribute('aria-label')) || 'pills';
    pills[key] = Array.from(ul.querySelectorAll('button, [role="radio"]')).map(b => ({ ref: mkref(b), text: norm(b.innerText || b.textContent),
      checked: b.getAttribute('aria-checked') === 'true' || b.getAttribute('aria-pressed') === 'true' }));
  }
  const save = host.querySelector('.save-btn'), cancel = host.querySelector('.cancel-btn');
  return JSON.stringify({ open: true, fields, pills, save: save ? { ref: mkref(save), text: norm(save.innerText) } : null,
    cancel: cancel ? { ref: mkref(cancel) } : null,
    errors: Array.from(f.querySelectorAll('.input-row--invalid')).map(r => norm(r.innerText)).filter(Boolean).slice(0, 6) });
}
"""

# A cx-select's list: its gridcells' texts.
JS_GRID = r"""
(lid) => {
  const box = document.getElementById(lid);
  if (!box) return '[]';
  return JSON.stringify(Array.from(box.querySelectorAll('[role="gridcell"]')).map((o, idx) => ({ idx, text: (o.innerText || o.textContent || '').replace(/\s+/g, ' ').trim() })).filter(o => o.text));
}
"""


# ---------------------------------------------------------------------------------------------------------- pure parts
def _norm(s: str) -> str:
    return answers.norm(s or "")


def same_text(a: str, b: str) -> bool:
    """Exactly the same words and punctuation; only runs of whitespace may differ. A tile or box that says 'Founding
    Software Engineer Part-time' is not his '(Part-time)' title (BNY's import, 2026-10-22)."""
    return re.sub(r"\s+", " ", a or "").strip() == re.sub(r"\s+", " ", b or "").strip()


def _month_years(text: str) -> list[tuple[int, int]]:
    from ..gate import _month_years as my
    return my(text)


def month_year(value: str) -> tuple[int, int] | None:
    d = fill.parse_date(value or "")
    return (int(d["month"]), int(d["year"])) if d.get("month") and d.get("year") else None


def tile_employer(t: dict) -> str:
    """The employer a work tile shows: its subtitle without the dates ('Revio 02/2026' -> 'Revio')."""
    return TILE_DATES.sub("", t.get("sub") or "").strip(" ,-")


def title_matches(shown: str, stored: str) -> bool:
    """'Software Engineering Intern' is the start of 'Software Engineering Intern, Quantitative Systems' (Oracle's import
    cut his title at the comma); 'Founding Software Engineer Part-time' is 'Founding Software Engineer (Part-time)'."""
    s, t = _norm(shown), _norm(stored)
    if not s or not t or UNNAMED.match(shown or ""):
        return False
    head = _norm(re.split(r"\s*[(,]", stored or "", maxsplit=1)[0])
    return s == t or s == head or t.startswith(s + " ")


def tile_dates(t: dict) -> list[tuple[int, int]]:
    return _month_years(f"{t.get('sub') or ''} {t.get('dates') or ''}")


def dates_fit(t: dict, e: dict) -> bool:
    """The tile's dates do not contradict the entry's (a tile without dates fits any)."""
    shown, start, end = tile_dates(t), month_year(e.get("start")), month_year(e.get("end"))
    if not shown:
        return True
    return start is not None and shown[0] == start and (len(shown) < 2 or end is None or shown[1] == end)


def dates_exact(t: dict, start: tuple | None, end: tuple | None) -> bool:
    shown = tile_dates(t)
    want = [d for d in (start, end) if d]
    return shown == want


def match_work(tiles: list[dict], stored: list[dict]) -> tuple[list[tuple[dict, dict]], list[tuple[dict, str]], list[dict]]:
    """-> ([(tile, his entry)], [(tile, why it goes)], [entries with no tile]). A tile is his entry by its employer, else
    by its job title (dates agreeing), else by its dates alone ('Unnamed Job Title' over 10/2025 - 12/2025 is Southwest)."""
    pair: dict[int, dict] = {}
    taken: set[int] = set()

    def first(pred):
        return next((e for e in stored if e["n"] not in taken and pred(e)), None)

    for test in (lambda t, e: bool(tile_employer(t)) and _same_company(tile_employer(t), e["company"]),
                 lambda t, e: title_matches(t["title"], e["title"]) and dates_fit(t, e),
                 lambda t, e: bool(tile_dates(t)) and dates_fit(t, e) and (len(tile_dates(t)) >= 2 or not month_year(e.get("end")))):
        for t in tiles:
            if t["idx"] in pair:
                continue
            e = first(lambda e, t=t: test(t, e))
            if e is not None:
                pair[t["idx"]] = e
                taken.add(e["n"])
    kept = [(t, pair[t["idx"]]) for t in tiles if t["idx"] in pair]
    doomed = []
    for t in tiles:
        if t["idx"] in pair:
            continue
        twin = next((e for e in stored if (tile_employer(t) and _same_company(tile_employer(t), e["company"]))
                     or title_matches(t["title"], e["title"])), None)
        doomed.append((t, f"a second entry for {twin['company']}" if twin else "none of his stored roles"))
    return kept, doomed, [e for e in stored if e["n"] not in taken]


def work_tile_right(t: dict, e: dict) -> bool:
    """Everything the tile shows equals his entry: title, employer, start and end (none for a current role)."""
    if t.get("invalid"):
        return False
    return (same_text(t["title"], e["title"]) and same_text(tile_employer(t), e["company"])
            and dates_exact(t, month_year(e["start"]), None if e["current"] else month_year(e["end"])))


def match_education(tiles: list[dict], names: list[str], major: str, high_school: str) -> tuple[dict | None, list[tuple[dict, str]]]:
    """-> (his degree entry, [(entry, why it goes)]). His degree entry names his major as its title, or his school with a
    degree; his high school stays; anything else ('Engineering Honors / Texas A&M University') goes."""
    keys = [k for k in (_norm(n) for n in names) if k]
    mine, doomed = None, []
    for t in tiles:
        both = f"{t.get('title') or ''} {t.get('sub') or ''}"
        text = _norm(both)
        if high_school and _norm(high_school) and _norm(high_school) in text:
            continue
        his = (major and _norm(t.get("title")) == _norm(major)) or (any(k in text for k in keys) and HIS_DEGREE.search(both))
        if his and mine is None:
            mine = t
        else:
            doomed.append((t, "a second entry for his degree" if his else "not his education entry"))
    return mine, doomed


def edu_tile_right(t: dict, names: list[str], major: str, start: tuple | None, grad: tuple | None) -> bool:
    if t.get("invalid") or (major and not same_text(t.get("title"), major)):
        return False
    sub = _norm(t.get("sub"))
    return any(_norm(n) and _norm(n) in sub for n in names) and dates_exact(t, start, grad)


def degree_option(options: list[str], degree: str) -> str | None:
    """'Bachelor of Science' among Oracle's degree lists: the same wording, else a bachelor of science, else the generic
    bachelor's entry (GM Financial lists 'Bachelors')."""
    want = _norm(degree)
    for o in options:
        if _norm(o) == want:
            return o
    for o in options:
        n = _norm(o)
        if "bachelor" in n and "science" in n:
            return o
    for o in options:
        if GENERIC_BACHELOR.fullmatch(_norm(o)):
            return o
    return None


def first_equal(options: list[str], wants: list[str]) -> str | None:
    for w in wants:
        for o in options:
            if _norm(o) == _norm(w):
                return o
    return None


def city_of(location: str) -> str:
    """'College Station, TX' -> 'College Station'; 'Remote' -> ''."""
    loc = (location or "").strip()
    if not loc or re.fullmatch(r"remote|virtual|online|hybrid", loc, re.I):
        return ""
    return loc.split(",")[0].strip()


def signature(t: dict) -> str:
    return "|".join((t.get("kind") or "", _norm(t.get("title")), _norm(t.get("sub")), _norm(t.get("dates"))))


# ---------------------------------------------------------------------------------------------------------- the filler
class OracleSections:
    def __init__(self, ex, job: dict, page_no: int = 0):
        self.ex, self.job, self.page_no = ex, job, page_no
        self.page = ex.page
        self.company = job.get("company", "") or ""
        self.rep = SectionReport()
        # entry signatures this run opened and found (or made) right: a timeline entry shows no employer, so it is opened
        # once per run (the run's executor carries them), not on every snapshot
        self.verified: set[str] = ex.__dict__.setdefault("_oracle_verified", set())
        self.actions = 0
        self.dirty = False          # the open form was changed (an entry that already held every value is cancelled)

    # ------------------------------------------------------------------ reading
    async def scan(self) -> dict:
        return json.loads(await self.page.evaluate(JS_SCAN) or "{}")

    async def form(self) -> dict:
        return json.loads(await self.page.evaluate(JS_FORM) or "{}")

    async def applies(self) -> bool:
        """An Experience or Education block shows on this step and no entry form is open (the model may be working in
        one; the gate holds the page while it is)."""
        try:
            info = await self.scan()
        except Exception:
            return False
        return not info.get("form") and any(b["visible"] for b in info.get("blocks") or [])

    def _answer(self, key: str) -> str:
        a = answers.get(key, self.company, "oracle")
        return str(a.answer).strip() if a and a.has_value and a.answer is not None else ""

    def _visible_blocks(self, info: dict, kind: str) -> list[dict]:
        return [b for b in info.get("blocks") or [] if b["visible"] and b["kind"] in (kind, "mixed")]

    def _tiles(self, info: dict, kind: str) -> list[tuple[dict, dict]]:
        return [(b, t) for b in self._visible_blocks(info, kind) for t in b["tiles"] if t["kind"] == kind]

    # ------------------------------------------------------------------ primitives
    def _block_loc(self, b: dict):
        return self.page.locator(".apply-flow-block__form-list[aria-label]").nth(int(b["bidx"]))

    def _tile_loc(self, b: dict, t: dict):
        sel = ".beautiful-timeline-item[data-profile-item-tile]" if b["layout"] == "timeline" else "article.apply-flow-profile-item-tile"
        return self._block_loc(b).locator(sel).nth(int(t["idx"]))

    async def _click(self, loc) -> None:
        try:
            await loc.click(timeout=WAIT_MS)
        except Exception:
            # an icon that only shows on hover: the page's own click handler still runs
            await loc.evaluate("e => e.click()")

    async def _wait_form(self, want_open: bool, polls: int = 30) -> dict:
        st: dict = {}
        for _ in range(polls):
            await asyncio.sleep(0.2)
            st = await self.form()
            if bool(st.get("open")) == want_open:
                return st
        return st

    async def _timeline_action(self, item, icon: str) -> bool:
        """A timeline entry shows Edit / Remove while the pointer is on it."""
        for how in ("hover", "focus"):
            try:
                await (item.hover(timeout=WAIT_MS) if how == "hover" else item.focus(timeout=WAIT_MS))
                btn = item.locator(icon).first
                await btn.wait_for(state="visible", timeout=2000)
                await btn.click(timeout=WAIT_MS)
                return True
            except Exception:
                continue
        return False

    async def _open_entry(self, b: dict, t: dict) -> bool:
        self.actions += 1
        loc = self._tile_loc(b, t)
        if b["layout"] == "timeline":
            if not await self._timeline_action(loc, ".beautiful-timeline-item__edit-icon"):
                await loc.click(timeout=WAIT_MS)      # the entry itself opens its form on the standard apply flow (BNY)
        else:
            await self._click(loc.locator(".apply-flow-profile-item-tile__edit-item-icon").first)
        return bool((await self._wait_form(True)).get("open"))

    async def _delete_entry(self, b: dict, t: dict, kind: str) -> bool:
        self.actions += 1
        before = len(self._tiles(await self.scan(), kind))
        loc = self._tile_loc(b, t)
        if b["layout"] == "timeline":
            if not await self._timeline_action(loc, ".beautiful-timeline-item__delete-icon"):
                return False
        else:
            await self._click(loc.locator(".apply-flow-profile-item-tile__delete-icon").first)
        for _ in range(25):
            await asyncio.sleep(0.2)
            info = await self.scan()
            if info.get("form"):
                await self._cancel()       # the click opened the entry instead
                return False
            if len(self._tiles(info, kind)) < before:
                return True
        return False

    async def _open_add(self, kind: str) -> bool:
        self.actions += 1
        info = await self.scan()
        for b in self._visible_blocks(info, kind):
            if not b["add"].get(kind):
                continue
            blk = self._block_loc(b)
            sel = (("button.timeline-work-add-button" if kind == "work" else "button.timeline-education-add-button")
                   if b["layout"] == "timeline" else "button.apply-flow-profile-item-tile__new-tile")
            await self._click(blk.locator(sel).first)
            return bool((await self._wait_form(True)).get("open"))
        return False

    async def _save(self) -> tuple[bool, list[str]]:
        st = await self.form()
        if not st.get("open") or not st.get("save"):
            return False, ["no entry form with a Save button is open"]
        await self.page.locator(f'[data-goat-ref="{st["save"]["ref"]}"]').click(timeout=WAIT_MS)
        for i in range(40):
            await asyncio.sleep(0.2)
            st = await self.form()
            if not st.get("open"):
                return True, []
            if st.get("errors") and i >= 5:
                break                     # its own check refused it: no need to wait out the server round trip
        return False, st.get("errors") or ["the form stayed open after Save"]

    async def _cancel(self) -> None:
        st = await self.form()
        if st.get("open") and st.get("cancel"):
            try:
                await self.page.locator(f'[data-goat-ref="{st["cancel"]["ref"]}"]').click(timeout=WAIT_MS)
            except Exception:
                await self.page.locator(f'[data-goat-ref="{st["cancel"]["ref"]}"]').evaluate("e => e.click()")
            await self._wait_form(False, polls=20)

    async def _set_text(self, field: dict, value: str) -> bool:
        if same_text(field.get("value"), value):
            return True
        self.dirty = True
        try:
            out = await fill.fill_batch(self.ex, [{"ref": field["ref"], "value": value}])
            if out and out[0].ok and same_text(await self._value(field), value):
                return True
        except Exception:
            pass
        loc = self.page.locator(f'[data-goat-ref="{field["ref"]}"]')
        await loc.fill(value, timeout=WAIT_MS)
        await loc.evaluate("e => { e.dispatchEvent(new Event('input', {bubbles: true})); e.dispatchEvent(new Event('change', {bubbles: true})); }")
        return same_text(await self._value(field), value)

    async def _value(self, field: dict) -> str:
        return await self.page.locator(f'[data-goat-ref="{field["ref"]}"]').input_value(timeout=WAIT_MS)

    async def _check(self, field: dict, want: bool) -> bool:
        box = self.page.locator(f'[data-goat-ref="{field["ref"]}"]')
        if await box.is_checked() == want:
            return True
        self.dirty = True
        lab = self.page.locator(f'label[for="{field["id"]}"]')
        try:
            await lab.first.click(timeout=WAIT_MS)
        except Exception:
            await box.evaluate("e => e.click()")
        for _ in range(15):
            await asyncio.sleep(0.15)
            if await box.is_checked() == want:
                return True
        return False

    async def _grid(self, field: dict) -> list[dict]:
        return json.loads(await self.page.evaluate(JS_GRID, field["id"] + "-listbox") or "[]")

    async def _pick(self, field: dict, wants: list[str], search: str = "", chooser=None) -> str:
        """Oracle's cx-select: open its list (its toggle shows the whole list; typing filters it, and the School list is a
        server search), click the option `chooser` (or exact wording) picks, -> what the box shows then ('' if none)."""
        choose = chooser or (lambda opts: first_equal(opts, wants))
        if field.get("value") and choose([field["value"]]):
            return field["value"]
        box = self.page.locator(f'[data-goat-ref="{field["ref"]}"]')
        hit, opts = None, []
        tog = self.page.locator(f'[id="{field["id"]}-toggle-button"]')
        if await tog.count():
            try:
                await tog.first.click(timeout=WAIT_MS)
            except Exception:
                await tog.first.evaluate("e => e.click()")
            for _ in range(10):
                await asyncio.sleep(0.2)
                opts = await self._grid(field)
                if opts:
                    break
            hit = choose([o["text"] for o in opts])
        if hit is None:
            await box.click(timeout=WAIT_MS)
            await self.page.keyboard.press("Control+a")
            await self.page.keyboard.press("Backspace")
            # Oracle's search-as-you-type boxes drop keys typed faster than ~20 ms apart (AutoZone, 2026-10-19)
            await self.page.keyboard.type(search or (wants[0] if wants else ""), delay=45)
            for _ in range(30):
                await asyncio.sleep(0.2)
                opts = await self._grid(field)
                hit = choose([o["text"] for o in opts])
                if hit is not None:
                    break
        if hit is None:
            await self.page.keyboard.press("Escape")
            return ""
        idx = next(o["idx"] for o in opts if o["text"] == hit)
        self.dirty = True
        cell = self.page.locator(f'[id="{field["id"]}-listbox"] [role="gridcell"]').nth(int(idx))
        try:
            await cell.click(timeout=WAIT_MS)
        except Exception:
            await cell.evaluate("e => e.click()")
        got = ""
        for _ in range(15):
            await asyncio.sleep(0.2)
            got = await box.input_value()
            if _norm(got) == _norm(hit):
                return got
        return got if choose([got]) else ""

    async def _clear_pick(self, field: dict) -> bool:
        self.dirty = True
        reset = self.page.locator(f'[id="{field["id"]}-reset-button"]')
        if await reset.count():
            try:
                await reset.first.click(timeout=WAIT_MS)
            except Exception:
                await reset.first.evaluate("e => e.click()")
        else:
            box = self.page.locator(f'[data-goat-ref="{field["ref"]}"]')
            await box.click(timeout=WAIT_MS)
            await self.page.keyboard.press("Control+a")
            await self.page.keyboard.press("Backspace")
            await self.page.keyboard.press("Tab")
        await asyncio.sleep(0.3)
        return not (await self._value(field)).strip()

    async def _date(self, which: str, value: str) -> bool | None:
        """Month and Year pickers of Start / End Date: True when both show the stored month and year, None when the form
        has no such date."""
        fl = (await self.form()).get("fields") or {}
        mon, yr = fl.get(f"month-{which}Date"), fl.get(f"year-{which}Date")
        if mon is None or yr is None or not (mon.get("vis") and yr.get("vis")):
            return None
        my = month_year(value)
        if my is None:
            return False
        name = MONTHS[my[0] - 1]
        got = await self._pick(mon, [name, name[:3], f"{my[0]:02d}", str(my[0])], search=name)
        if not got:
            return False
        got = await self._pick(yr, [str(my[1])], search=str(my[1]))
        return bool(got)

    # ------------------------------------------------------------------ one entry form
    async def _write_work(self, ent: dict) -> tuple[bool, str, list[tuple[str, str, str]]]:
        """With the entry form open: every field from experience.work.N. -> (ok, why not, [(field, value, key)])."""
        fl = (await self.form()).get("fields") or {}
        done: list[tuple[str, str, str]] = []
        for name, key, label in (("employerName", "company", "Employer Name"), ("jobTitle", "title", "Job Title")):
            f = fl.get(name)
            if f is None:
                return False, f"the form has no {label} box", done
            if not await self._set_text(f, ent[key]):
                return False, f"{label} did not take {ent[key]!r}", done
            done.append((label, ent[key], key))
        r = await self._date("start", ent["start"])
        if r is False:
            return False, f"Start Date did not take {ent['start']!r}", done
        if r:
            done.append(("Start Date", ent["start"], "start"))
        fl = (await self.form()).get("fields") or {}
        cur = fl.get("currentJobFlag")
        if cur is not None:
            if not await self._check(cur, bool(ent["current"])):
                return False, "Current Job did not tick" if ent["current"] else "Current Job did not clear", done
            if ent["current"]:
                done.append(("Current Job", "Yes", "current"))
        if ent["current"]:
            # a current role has no end date (the tenant without a Current Job box shows his start alone, GM Financial)
            fl = (await self.form()).get("fields") or {}
            for name in ("month-endDate", "year-endDate"):
                f = fl.get(name)
                if f and f.get("vis") and f.get("value") and not await self._clear_pick(f):
                    return False, "the End Date of a current role did not clear", done
        elif ent.get("end"):
            r = await self._date("end", ent["end"])
            if r is False:
                return False, f"End Date did not take {ent['end']!r}", done
            if r:
                done.append(("End Date", ent["end"], "end"))
        fl = (await self.form()).get("fields") or {}
        city, f = city_of(ent.get("location", "")), fl.get("employerCity")
        if f is not None and city and await self._set_text(f, city):
            done.append(("Employer City", city, "location"))
        f = next((fl[n] for n in DESC_NAMES if n in fl), None)
        if f is not None and ent.get("description"):
            desc = bullets_to_lines(ent["description"])
            cap = f.get("max") or DESC_CAP
            if len(desc) > cap:
                desc = fit_bullets(desc, cap)
                self.rep.skipped.append(f"{ent['company']}: description trimmed to the box's {cap}-character cap")
            if not await self._set_text(f, desc):
                return False, "Responsibilities did not take the description", done
            done.append(("Responsibilities", desc, "description"))
        return True, "", done

    async def _write_education(self) -> tuple[bool, str, list[tuple[str, str, str]]]:
        """With the entry form open: school, degree, major, dates and Graduated from education.*."""
        school, major, degree = self._answer("education.school"), self._answer("education.major"), self._answer("education.degree")
        start, grad = self._answer("education.start"), self._answer("education.graduation")
        names = [school] + [v.strip() for v in self._answer("education.school_variants").split(";") if v.strip()]
        fl = (await self.form()).get("fields") or {}
        done: list[tuple[str, str, str]] = []
        f = next((fl[n] for n in SCHOOL_NAMES if n in fl), None)
        if f is not None and school:
            got = await self._pick(f, names, search=school, chooser=lambda opts: first_equal(opts, names))
            if not got:
                # the long name finds nothing on some tenants; the short one lists it (Sierra, 2026-10-19)
                got = await self._pick(f, names, search=" ".join(school.split()[:2]), chooser=lambda opts: first_equal(opts, names))
            if not got:
                # his own rule: "Try Texas A&M, otherwise, pick other"
                got = await self._pick(f, ["Other"], search="Other")
                if got:
                    other = next((v for n, v in ((await self.form()).get("fields") or {}).items() if re.search(r"other", n, re.I) and v.get("tag") == "input" and not v.get("combo")), None)
                    if other is not None:
                        await self._set_text(other, school)
            if not got:
                return False, f"School: no {school!r} and no 'Other' in its list", done
            done.append(("School", got, "school"))
        elif school:
            # a form without the School picker names the school in a text box ('Educator' on BNY's, 2026-10-22)
            f = next((v for n, v in fl.items() if re.fullmatch(r"educator|school(name)?|institution(name)?", n, re.I)
                      and v.get("tag") == "input" and not v.get("combo")), None)
            if f is not None:
                if not await self._set_text(f, school):
                    return False, f"{f.get('label') or 'Educator'} did not take {school!r}", done
                done.append(("School", school, "school"))
        st = await self.form()
        fl = st.get("fields") or {}
        f = fl.get("contentItemId") or next((v for n, v in fl.items() if re.search(r"degree", n, re.I)), None)
        pills = next((v for k, v in (st.get("pills") or {}).items() if re.search(r"degree", k, re.I)), None)
        if f is not None and degree:
            got = await self._pick(f, [degree], search="Bach", chooser=lambda opts: degree_option(opts, degree))
            if not got:
                return False, f"Degree: nothing like {degree!r} in its list", done
            done.append(("Degree", got, "degree"))
        elif pills and degree:
            # BNY's Degree is a row of single-select pills ('Bachelor's Degree', 'Master's Degree', ...), not a picker
            hit = degree_option([p["text"] for p in pills], degree)
            if hit is None:
                return False, f"Degree: nothing like {degree!r} among its choices", done
            pill = next(p for p in pills if p["text"] == hit)
            if not pill["checked"]:
                self.dirty = True
                await self._click(self.page.locator(f'[data-goat-ref="{pill["ref"]}"]'))
                for _ in range(15):
                    await asyncio.sleep(0.2)
                    now = next((v for k, v in ((await self.form()).get("pills") or {}).items() if re.search(r"degree", k, re.I)), [])
                    if any(p["text"] == hit and p["checked"] for p in now):
                        break
                else:
                    return False, f"Degree: {hit!r} did not take", done
            done.append(("Degree", hit, "degree"))
        f = fl.get("major")
        if f is not None and major:
            if not await self._set_text(f, major):
                return False, f"Major did not take {major!r}", done
            done.append(("Major", major, "major"))
        for which, val, key in (("start", start, "start"), ("end", grad, "graduation")):
            if not val:
                continue
            r = await self._date(which, val)
            if r is False:
                return False, f"{which.title()} Date did not take {val!r}", done
            if r:
                done.append((f"{which.title()} Date", val, key))
        f = ((await self.form()).get("fields") or {}).get("graduatedFlag")
        if f is not None:
            # he graduates in May 2028: an imported entry came back ticked (GM Financial, 2026-10-22)
            if not await self._check(f, False):
                return False, "Graduated did not clear", done
            done.append(("Graduated", "No", "graduation"))
        return True, "", done

    def _record(self, what: str, done: list[tuple[str, str, str]], prefix: str) -> None:
        for label, value, key in done:
            records.record_decision(self.job["id"], self.page_no, f"{what}: {label}", value, f"standing:{prefix}.{key}", "")

    async def _finish(self, ok: bool, why: str) -> tuple[bool, str]:
        if not ok:
            await self._cancel()
            return False, why
        saved, errs = await self._save()
        if not saved:
            await self._cancel()
            return False, "Save was refused: " + "; ".join(errs)[:200]
        return True, ""

    # ------------------------------------------------------------------ work entries
    async def work(self) -> None:
        stored = stored_work_entries(self.company, "oracle")
        if not stored:
            self.rep.skipped.append("experience: no stored work entries")
            return
        # 1. entries that are none of his go, one at a time (the tiles re-render after each)
        while self.actions < MAX_ACTIONS:
            tiles = self._tiles(await self.scan(), "work")
            _, doomed, _ = match_work([t for _, t in tiles], stored)
            if not doomed:
                break
            t, why = doomed[0]
            b = next(b for b, x in tiles if x is t)
            label = t["title"] or tile_employer(t) or "(empty entry)"
            if await self._delete_entry(b, t, "work"):
                self.rep.updated.append(f"experience '{label}' removed: {why}")
            else:
                self.rep.failed.append(f"experience '{label}' could not be deleted ({why})")
                break
        # 2. his own entries: opened and set where anything differs
        for ent in stored:
            if self.actions >= MAX_ACTIONS:
                break
            tiles = self._tiles(await self.scan(), "work")
            kept, _, _ = match_work([t for _, t in tiles], stored)
            t = next((t for t, e in kept if e["n"] == ent["n"]), None)
            if t is None:
                continue
            b = next(b for b, x in tiles if x is t)
            if work_tile_right(t, ent) or signature(t) in self.verified:
                self.rep.present.append(f"experience: {ent['company']}")
                continue
            if not await self._open_entry(b, t):
                self.rep.failed.append(f"experience: {ent['company']}: its entry did not open")
                continue
            self.dirty = False
            ok, why, done = await self._write_work(ent)
            if ok and not self.dirty:
                await self._cancel()          # it already held every stored value: nothing to save
                self.rep.present.append(f"experience: {ent['company']}")
                await self._remember("work", ent)
                continue
            ok, why = await self._finish(ok, why)
            if not ok:
                self.rep.failed.append(f"experience: {ent['company']}: {why}")
                continue
            self._record(f"Experience {ent['company']}", done, f"experience.work.{ent['n']}")
            self.rep.updated.append(f"experience: {ent['title']} at {ent['company']}")
            await self._remember("work", ent)
        # 3. the missing ones are added
        tiles = self._tiles(await self.scan(), "work")
        _, _, missing = match_work([t for _, t in tiles], stored)
        for ent in missing:
            if self.actions >= MAX_ACTIONS:
                self.rep.failed.append(f"experience: {ent['company']}: not added (too many steps in one pass)")
                continue
            if not await self._open_add("work"):
                self.rep.failed.append(f"experience: {ent['company']}: Add Experience did not open a form")
                continue
            ok, why, done = await self._write_work(ent)
            ok, why = await self._finish(ok, why)
            if not ok:
                self.rep.failed.append(f"experience: {ent['company']}: {why}")
                continue
            self._record(f"Experience {ent['company']}", done, f"experience.work.{ent['n']}")
            self.rep.added.append(f"experience: {ent['title']} at {ent['company']}")
            await self._remember("work", ent)

    async def _remember(self, kind: str, ent: dict | None) -> None:
        """The entry just saved is right: its tile's signature is remembered so a timeline entry (no employer shown) is
        not opened again on the next snapshot."""
        tiles = [t for _, t in self._tiles(await self.scan(), kind)]
        if kind == "work" and ent is not None:
            kept, _, _ = match_work(tiles, stored_work_entries(self.company, "oracle"))
            t = next((t for t, e in kept if e["n"] == ent["n"]), None)
        else:
            t, _ = match_education(tiles, self._school_names(), self._answer("education.major"), self._answer("education.high_school"))
        if t is not None:
            self.verified.add(signature(t))

    # ------------------------------------------------------------------ education
    def _school_names(self) -> list[str]:
        school = self._answer("education.school")
        return [n for n in [school] + [v.strip() for v in self._answer("education.school_variants").split(";")] if n]

    async def education(self) -> None:
        names, major = self._school_names(), self._answer("education.major")
        if not names:
            self.rep.skipped.append("education: no stored school")
            return
        high_school = self._answer("education.high_school")
        start, grad = month_year(self._answer("education.start")), month_year(self._answer("education.graduation"))
        while self.actions < MAX_ACTIONS:
            tiles = self._tiles(await self.scan(), "education")
            _, doomed = match_education([t for _, t in tiles], names, major, high_school)
            if not doomed:
                break
            t, why = doomed[0]
            b = next(b for b, x in tiles if x is t)
            label = t["title"] or "(empty entry)"
            if await self._delete_entry(b, t, "education"):
                self.rep.updated.append(f"education '{label}' removed: {why}")
            else:
                self.rep.failed.append(f"education '{label}' could not be deleted ({why})")
                break
        tiles = self._tiles(await self.scan(), "education")
        mine, _ = match_education([t for _, t in tiles], names, major, high_school)
        if mine is not None and (edu_tile_right(mine, names, major, start, grad) or signature(mine) in self.verified):
            self.rep.present.append(f"education: {names[0]}")
            return
        if self.actions >= MAX_ACTIONS:
            self.rep.failed.append(f"education: {names[0]}: not written (too many steps in one pass)")
            return
        if mine is not None:
            b = next(b for b, x in tiles if x is mine)
            if not await self._open_entry(b, mine):
                self.rep.failed.append(f"education: {names[0]}: its entry did not open")
                return
        elif not await self._open_add("education"):
            blocks = self._visible_blocks(await self.scan(), "education")
            if any(b["required"] for b in blocks):
                self.rep.failed.append(f"education: {names[0]}: Add Education did not open a form")
            else:
                self.rep.skipped.append("education: no Add Education here")
            return
        self.dirty = False
        ok, why, done = await self._write_education()
        if ok and not self.dirty and mine is not None:
            await self._cancel()
            self.rep.present.append(f"education: {names[0]}")
            await self._remember("education", None)
            return
        ok, why = await self._finish(ok, why)
        if not ok:
            self.rep.failed.append(f"education: {names[0]}: {why}")
            return
        self._record(f"Education {names[0]}", done, "education")
        (self.rep.updated if mine is not None else self.rep.added).append(f"education: {names[0]}")
        await self._remember("education", None)

    # ------------------------------------------------------------------
    async def run(self) -> SectionReport:
        try:
            info = await self.scan()
        except Exception as e:  # noqa: BLE001 - a page that cannot be read is reported, never raised
            self.rep.failed.append(f"scan: {type(e).__name__}: {str(e).splitlines()[0][:120]}")
            return self.rep
        if not any(b["visible"] for b in info.get("blocks") or []):
            self.rep.skipped.append("no Oracle Experience / Education block on this step")
            return self.rep
        if info.get("form"):
            # a form left open (by the model or an earlier pass) is closed; its entry is redone from the store
            try:
                await self._cancel()
            except Exception:
                pass
        for kind, step in (("experience", self.work), ("education", self.education)):
            if not self._visible_blocks(await self.scan(), "work" if kind == "experience" else "education"):
                continue
            try:
                await step()
            except Exception as e:  # noqa: BLE001 - one section's trouble never stops the other
                self.rep.failed.append(f"{kind}: {type(e).__name__}: {str(e).splitlines()[0][:120]}")
                try:
                    await self._cancel()
                except Exception:
                    pass
        return self.rep
