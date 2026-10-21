"""fill_batch / choose / upload_resume with per-field read-back. Plain controls are set through the
page's own setters in one round trip (Hermes measured 38 -> 10 tool calls on a 28-field form);
custom dropdowns are driven with real mouse and keyboard input and verified by read-back."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from . import js
from .executor import Executor, ClickRefused

PLACEHOLDER = re.compile(r"^(select one|select\.\.\.|select…|select|choose one|choose|search|please select|no selection|--|)$", re.I)


@dataclass
class FieldResult:
    ref: str
    ok: bool
    readback: str
    error: str = ""

    def as_dict(self) -> dict:
        return {"ref": self.ref, "status": "set" if self.ok else "failed", "readback": self.readback, "error": self.error}


# Portals that store every answer with its own background request the moment it changes. Two saves in flight at
# once lose one of them (Sierra, 2026-10-19: four radios ticked in one script call, two never stored, and the
# submit came back "Missing entry for required field" while the page showed all four).
AUTOSAVE_HOSTS = ("ashbyhq.com",)


def autosaves(ex: Executor) -> bool:
    url = (getattr(ex, "url", "") or "").lower()
    return any(h in url for h in AUTOSAVE_HOSTS)


async def ashby_resave(ex: Executor) -> list[dict]:
    """Make an autosaving form store the answers it shows but lost: a radio is ticked to another option and back
    (each save awaited), a text box is focused and left. Returns the fields still unsaved afterwards."""
    try:
        bad = await ex.eval(js.JS_ASHBY_UNSAVED)
    except Exception:
        return []
    bad = bad if isinstance(bad, list) else []
    if not bad:
        return []
    for b in bad:
        try:
            if b.get("kind") == "radio":
                r = await ex.eval(js.JS_ASHBY_REPICK, [b.get("path"), "away", -1])
                if isinstance(r, dict) and r.get("ok"):
                    await ex.wait_saves()
                    await ex.eval(js.JS_ASHBY_REPICK, [b.get("path"), "back", r.get("idx")])
                    await ex.wait_saves()
            elif b.get("kind") == "text":
                await ex.eval(js.JS_ASHBY_REPICK, [b.get("path"), "blur", -1])
                await ex.wait_saves()
        except Exception:
            continue
    try:
        left = await ex.eval(js.JS_ASHBY_UNSAVED)
    except Exception:
        left = []
    left = left if isinstance(left, list) else []
    try:
        from .. import db
        db.event(ex.job_id, "fill:resaved", {"fields": [b.get("title", "")[:60] for b in bad][:10],
                                             "still_unsaved": [b.get("title", "")[:60] for b in left][:10]})
    except Exception:
        pass
    return left


# Eightfold career sites keep their own copy of each answer and ignore a value set by script: Netflix's form showed 'Surya' in
# First name with "Error: First name cannot be left blank." and "4 errors found" (2026-10-21). Their text boxes are typed.
REAL_TYPING_HOSTS = re.compile(r"(^|\.)(eightfold\.ai|jobs\.netflix\.net|careers\.microsoft\.com)$", re.I)


# An Eightfold career site on the employer's own host (careers.qualcomm.com/careers/apply?pid=...&domain=qualcomm.com):
# the privacy box and the profile-use radio of its 'Please review before continuing' step were set by script, the page
# kept them unset ('You must agree to the Privacy Policy'), and four Qualcomm runs could not get past it (2026-10-21).
EIGHTFOLD_URL = re.compile(r"/careers(/apply)?/?\?(.*&)?pid=\d+.*\bdomain=", re.I)


JS_FOLDED_SECTIONS = """() => Array.from(document.querySelectorAll('button[id$=":topBar"][aria-expanded="false"]'))
  .filter(b => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
  .map(b => [b.id, (b.innerText || b.textContent || '').trim().slice(0, 60)])"""


async def unfold_sections(ex: Executor) -> list[str]:
    """Open SAP SuccessFactors' folded form sections, one real click on each section's header button. A folded section
    holds nothing the run has filled, so opening it loses nothing (CenturyLink, 2026-10-21)."""
    try:
        found = await ex.eval(JS_FOLDED_SECTIONS)
    except Exception:
        return []
    opened = []
    for bid, name in (found or [])[:12]:
        try:
            await ex.page.locator(f'[id="{bid}"]').first.click(timeout=4000)
            opened.append(str(name or bid))
            await asyncio.sleep(0.4)
        except Exception:
            continue
    if opened:
        try:
            await ex.settle(quiet_ms=400)
        except Exception:
            pass
    return opened


def needs_real_typing(ex: Executor) -> bool:
    from urllib.parse import urlparse
    url = getattr(ex, "url", "") or ""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        host = ""
    return bool(REAL_TYPING_HOSTS.search(host)) or bool(EIGHTFOLD_URL.search(url))


def _wants_checked(value) -> bool | None:
    if isinstance(value, bool):
        return value
    v = str(value or "").strip().lower()
    if v in ("true", "checked", "yes", "on", "1"):
        return True
    if v in ("false", "unchecked", "no", "off", "0", ""):
        return False
    return None


async def _click_boxes(ex: Executor, fields: list[dict]) -> list[FieldResult]:
    """Set tick boxes and radios with a real click (the page's own handlers run), then read them back."""
    out = []
    for f in fields:
        ref, want = str(f["ref"]), _wants_checked(f.get("value"))
        try:
            now = (await ex.read_ref(ref)).get("value") == "checked"
            if now != want:
                loc = ex.page.locator(f'[data-goat-ref="{ref}"]').first
                try:
                    await loc.click(timeout=5000)
                except Exception:
                    await loc.click(timeout=5000, force=True)
                await asyncio.sleep(0.3)
                now = (await ex.read_ref(ref)).get("value") == "checked"
            out.append(FieldResult(ref=ref, ok=now == want, readback="checked" if now else "unchecked",
                                   error="" if now == want else "the box did not take the click"))
        except Exception as e:  # noqa: BLE001 - reported per field
            out.append(FieldResult(ref=ref, ok=False, readback="", error=f"click failed: {type(e).__name__}"))
    return out


async def _type_boxes(ex: Executor, fields: list[dict]) -> list[FieldResult]:
    """Clear and type each plain text box with real keystrokes, leave it with Tab, read it back."""
    out = []
    for f in fields:
        ref, value = str(f["ref"]), str(f["value"])
        try:
            await ex.focus_ref(ref)
            await ex.page.keyboard.press("Control+a")
            await ex.page.keyboard.press("Backspace")
            await ex.type_text(value, real_keys=True, delay_ms=8)
            await ex.page.keyboard.press("Tab")
            await asyncio.sleep(0.15)
            now = str((await ex.read_ref(ref)).get("value") or "")
            out.append(FieldResult(ref=ref, ok=now.strip() == value.strip(), readback=now[:300],
                                   error="" if now.strip() == value.strip() else f"the box reads {now[:60]!r} after typing"))
        except Exception as e:  # noqa: BLE001 - reported per field
            out.append(FieldResult(ref=ref, ok=False, readback="", error=f"typing failed: {type(e).__name__}"))
    return out


async def fill_batch(ex: Executor, fields: list[dict]) -> list[FieldResult]:
    """fields: [{ref, value}] where value is str or bool. Password and file inputs are refused. On an autosaving
    form (Ashby) the fields are set one at a time, each save awaited, and lost saves are redone at the end."""
    if not fields:
        return []
    if needs_real_typing(ex):
        typed, clicked, rest = [], [], []
        for f in fields:
            v = f.get("value")
            try:
                info = await ex.read_ref(str(f["ref"]))
            except Exception:
                info = {}
            kind = str(info.get("type") or "").lower()
            want = _wants_checked(v)
            if info.get("tag") == "input" and kind in ("checkbox", "radio") and want is not None and (kind == "checkbox" or want):
                clicked.append(f)
                continue
            if isinstance(v, str) and v and "\n" not in v and len(v) <= 400:
                # never a picker's search box: Netflix's veteran question (2026-10-21) got his whole answer list typed into
                # it ('No match found for I am not a protected veteran; ...'); choose works those
                if (info.get("tag") in ("input", "textarea") and not info.get("combo") and info.get("expanded") is None
                        and str(info.get("type") or "text").lower() in ("text", "email", "tel", "url", "search", "number", "textarea", "")):
                    typed.append(f)
                    continue
            rest.append(f)
        if typed or clicked:
            done = (await _type_boxes(ex, typed) if typed else []) + (await _click_boxes(ex, clicked) if clicked else [])
            return done + (await fill_batch_js(ex, rest) if rest else [])
    return await fill_batch_js(ex, fields)


async def fill_batch_js(ex: Executor, fields: list[dict]) -> list[FieldResult]:
    """fill_batch through the page's own setters (one round trip)."""
    if not fields:
        return []
    serial = autosaves(ex)
    if serial:
        raw = []
        for f in fields:
            one = await ex.eval(js.JS_FILL_BATCH, [f])
            one = one if isinstance(one, list) else []
            raw += one
            if any(r.get("ok") and not r.get("real_click") for r in one):
                await ex.wait_saves()
    else:
        raw = await ex.eval(js.JS_FILL_BATCH, fields)
    out = []
    for r in raw if isinstance(raw, list) else []:
        if r.get("real_click"):
            # a toggle button: press it like a person (pointer + mouse events at its centre), then read it back
            try:
                await ex.click_ref(str(r.get("ref", "")))
                await asyncio.sleep(0.3)
                if serial:
                    await ex.wait_saves()
                info = await ex.read_ref(str(r.get("ref", "")))
                on = bool(info.get("pressed"))
                r = {**r, "readback": "pressed" if on else "unpressed", "ok": on == bool(r.get("want"))}
            except ClickRefused as e:
                r = {**r, "ok": False, "error": str(e)}
        out.append(FieldResult(ref=r.get("ref", ""), ok=bool(r.get("ok")), readback=str(r.get("readback", "")),
                               error=str(r.get("error", ""))))
    if serial:
        left = await ashby_resave(ex)
        lost = {ref for b in left for ref in (b.get("refs") or [])}
        for fr in out:
            if fr.ref in lost and fr.ok:
                fr.ok = False
                fr.error = ("the page shows this answer but the form has not stored it: tick a different option, then this one "
                            "again (or re-type the text)")
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


AFFIRM_OPT = re.compile(r"^(yes\b|i (agree|consent|accept|authori[sz]e|acknowledge|understand|certify|confirm)\b|agree\b|accept\b|"
                        r"i am (authori[sz]ed|eligible|legally|able|permitted|a (u\.?s\.? )?citizen)\b|i have (work )?authori[sz]ation\b)", re.I)
NEG_OPT = re.compile(r"^(no\b|i do not\b|i don'?t\b|please do not\b|do not\b|don'?t\b|i decline\b|decline\b|not\b|never\b|"
                     r"i have never\b|i have not\b|i am not\b|i'?m not\b|none\b|i (will not|won'?t) (require|need)\b)", re.I)


def _squash(s: str) -> str:
    return _norm(s).replace(" ", "")


# What a Workday multi-level prompt's control reads while a category is open or nothing is picked yet.
LEVELISH = re.compile(r"(expanded|\b0 items selected\b|^search$)", re.I)


JS_TEXT_FOCUSED = """() => { const a = document.activeElement; return !!(a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA' || a.isContentEditable)); }"""


async def _text_box_focused(ex: Executor) -> bool:
    try:
        return bool(await ex.eval(JS_TEXT_FOCUSED))
    except Exception:
        return False

async def _select_all_text(ex: Executor) -> bool:
    """Ctrl+A only when a text box has focus: in a focused listbox it selects every option (a Workday
    multi-select took 30 SQL chips that way, 2026-10-18)."""
    if not await _text_box_focused(ex):
        return False
    try:
        await ex.page.keyboard.press("Control+a")
        return True
    except Exception:
        return False



# The value a picker holds when the ref is its toggle button or its emptied search box: the companion input in
# the same container (property first, then the attribute React leaves behind).
JS_COMPANION_VALUE = """(ref) => { const el = document.querySelector('[data-goat-ref="' + ref + '"]'); if (!el) return '';
  const val = (i) => (i.value || i.getAttribute('value') || '').trim();
  if (el.tagName === 'INPUT' && val(el)) return { from: 'input', value: val(el) };
  let n = el.parentElement;
  for (let i = 0; i < 3 && n; i++, n = n.parentElement) {
    const inp = Array.from(n.querySelectorAll('input[role="combobox"], input[aria-haspopup], input:not([type=hidden]):not([type=checkbox]):not([type=radio])')).find(x => x !== el && val(x));
    if (inp) return { from: 'input', value: val(inp) };
    const chip = n.querySelector('[class*="single-value" i], [data-automation-id="selectedItem"]');
    if (chip && chip.textContent.trim()) return { from: 'chip', value: chip.textContent.trim() };
  }
  return { from: '', value: '' }; }"""

JS_IS_WORKDAY_PROMPT = """(ref) => { const el = document.querySelector('[data-goat-ref="' + ref + '"]'); if (!el) return false;
  const a = el.getAttribute('data-automation-id') || '';
  if (el.tagName === 'INPUT') return /search|prompt/i.test(a) || !!el.closest('[data-automation-id*="prompt" i], [data-automation-id*="multiSelect" i], [data-automation-id*="selectWidget" i], [data-automation-id*="searchBox" i]');
  return !!el.closest('[data-automation-id="multiSelectContainer"]'); }"""

# The search box of a Workday multi-select, which lives inside the chip list once something is selected: it gets
# its own ref (<ref>i) so it can be focused and read like any other element.
JS_PROMPT_INPUT = """(ref) => { const el = document.querySelector('[data-goat-ref="' + ref + '"]'); if (!el) return null;
  if (el.tagName === 'INPUT') return ref;
  const box = el.closest('[data-automation-id="multiSelectContainer"], [data-automation-id^="formField"]') || el;
  const inp = box.querySelector('input:not([type="hidden"])');
  if (!inp) return null;
  const r = inp.getBoundingClientRect();
  if (r.width < 2 || r.height < 2) return null;
  if (!inp.getAttribute('data-goat-ref')) inp.setAttribute('data-goat-ref', ref + 'i');
  return inp.getAttribute('data-goat-ref'); }"""


async def _prompt_input(ex: Executor, ref: str) -> str:
    """The ref to type into: the element itself when it is the search box, else the box inside its container
    (clicking the container makes Workday show it)."""
    try:
        r = await ex.eval(JS_PROMPT_INPUT, ref)
        if r:
            return str(r)
        # the search box is hidden until the chip list (or its prompt icon) is clicked
        await ex.click_ref(ref)
        for _ in range(4):
            await asyncio.sleep(0.5)
            r = await ex.eval(JS_PROMPT_INPUT, ref)
            if r:
                return str(r)
        icon = await ex.eval("""(ref) => { const el = document.querySelector('[data-goat-ref="' + ref + '"]'); if (!el) return null;
          const box = el.closest('[data-automation-id="multiSelectContainer"], [data-automation-id^="formField"]') || el;
          const ic = box.querySelector('[data-automation-id*="prompt" i], [data-automation-id*="Icon" i], [aria-label*="prompt" i], button');
          if (!ic) return null; const r = ic.getBoundingClientRect(); return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) }; }""", ref)
        if isinstance(icon, dict):
            await ex.click_xy(int(icon["x"]), int(icon["y"]))
            for _ in range(4):
                await asyncio.sleep(0.5)
                r = await ex.eval(JS_PROMPT_INPUT, ref)
                if r:
                    return str(r)
        return ref
    except Exception:
        return ref


JS_REMOVE_CHIPS = """(args) => {
  const [ref, keepArg] = args;
  const keepList = Array.isArray(keepArg) ? keepArg : (keepArg ? [keepArg] : []);
  const el = document.querySelector('[data-goat-ref="' + ref + '"]'); if (!el) return 0;
  const box = el.closest('[data-automation-id^="formField"], [data-automation-id="multiSelectContainer"], .field, .form-group') || el.parentElement;
  const norm = (t) => (t || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  let n = 0;
  for (const item of Array.from(box.querySelectorAll('[data-automation-id="selectedItem"]'))) {
    const t = norm(item.textContent);
    if (keepList.some(k => k && t.includes(norm(k)))) continue;
    const x = item.querySelector('[data-automation-id="DELETE_charm"], [data-automation-id*="delete" i], button, [role="button"]');
    if (x) { x.click(); n++; }
  }
  return n;
}"""


async def _remove_wrong_chips(ex: Executor, ref: str, keep="") -> int:
    try:
        n = await ex.eval(JS_REMOVE_CHIPS, [ref, keep])
        if n:
            await asyncio.sleep(0.5)
        return int(n or 0)
    except Exception:
        return 0


def lang_safe(s: str) -> str:
    """'C++' -> 'cplusplus', 'C#' -> 'csharp', 'F#' -> 'fsharp': stripping punctuation must never turn them into 'C'
    (T-Mobile's Skills got a 'C' chip for C++, 2026-10-19)."""
    t = (s or "").lower()
    t = re.sub(r"c\s*\+\s*\+", "cplusplus", t)
    return re.sub(r"\b([cf])\s*#", r"\1sharp", t)


def _chip_matches(chip: str, wants: list[str]) -> bool:
    """A selected chip equals one of the wanted wordings, or is one of them plus a parenthetical qualifier
    ("Python (Programming Language)" for "Python")."""
    chip = ENUM_PREFIX.sub("", chip or "", count=1) if re.match(r"^\s*\d{1,2}[.)]\s+\D", chip or "") else chip
    c = _norm(lang_safe(chip))
    for w in wants:
        if not w:
            continue
        if c == _norm(lang_safe(w)) or _squash(lang_safe(chip)) == _squash(lang_safe(w)):
            return True
        if re.fullmatch(re.escape(w.strip().lower()) + r"\s*(\(.*\))?", (chip or "").strip().lower()):
            return True
        # "C++ Programming Language" for C++ (T-Mobile's Skills, 2026-10-19): the wanted name plus that qualifier unbracketed
        if re.fullmatch(re.escape(w.strip().lower()) + r"\s+programming language", (chip or "").strip().lower()):
            return True
        # "AWS Certified Cloud Practitioner - Amazon Web Services (AWS)": the wanted name plus an issuer after a dash
        if re.fullmatch(re.escape(w.strip().lower()) + r"\s*[-–—:]\s+.+", (chip or "").strip().lower()):
            return True
        # "Structured Query Language (SQL)" for SQL: the acronym rule, but not when the parenthetical belongs to an
        # issuer after a dash ("AWS Business Professional - Amazon Web Services (AWS)" is not AWS)
        if re.search(r"\(\s*" + re.escape(w.strip()) + r"\s*\)", chip or "", re.I) and not re.search(r"\s[-–—]\s", chip or ""):
            return True
    return False


async def _type_and_enter(ex: Executor, ref: str, wants: list[str], baseline_keys: set) -> tuple[bool, str]:
    """Workday search prompts run their search on Enter: type the first wording, press Enter, then either a
    chip appeared (a single result or a free-entry tenant) or a result list did. A chip or result counts only
    when it matches one of the wanted wordings (exact, or with a parenthetical qualifier); anything else is
    removed again, never left on the form."""
    text = next((w for w in wants if w and w.strip()), "")
    if not text:
        return False, "nothing to type"
    try:
        st0 = await ex.eval(js.JS_FIELD_COMMIT, ref)
        chips_before = [c for c in (st0.get("chips") or []) if c] if isinstance(st0, dict) else []
    except Exception:
        chips_before = []
    for c in chips_before:
        if _chip_matches(c, wants):
            return True, c          # already selected (in any wanted wording): a second pick would only remove it
    try:
        await ex.page.keyboard.press("Escape")          # a results list from the previous term would be read as new
        await asyncio.sleep(0.3)
        base_now = await ex.eval(js.JS_LIST_OPTIONS)
        baseline_keys = set(baseline_keys) | {(o.get("text"), o.get("x"), o.get("y")) for o in (base_now if isinstance(base_now, list) else [])}
        input_ref = await _prompt_input(ex, ref)
        await ex.focus_ref(input_ref)
        if not await _text_box_focused(ex):
            # chips sit in front of the search box: a mouse click lands on them; focus the box directly
            await ex.eval("""(ref) => { const el = document.querySelector('[data-goat-ref="' + ref + '"]'); if (!el) return false; el.focus(); return document.activeElement === el; }""", input_ref)
            await asyncio.sleep(0.3)
        # A results list still open from an earlier search makes Enter pick its highlighted row instead of
        # searching: close it, then re-open the prompt through its icon so the box searches again.
        stale = await ex.eval(js.JS_LIST_OPTIONS)
        if isinstance(stale, list) and stale:
            await ex.page.keyboard.press("Escape")
            await asyncio.sleep(0.4)
            icon = await ex.eval("""(ref) => { const el = document.querySelector('[data-goat-ref="' + ref + '"]'); if (!el) return null;
              const box = el.closest('[data-automation-id="multiSelectContainer"], [data-automation-id^="formField"]') || el;
              const ic = box.querySelector('[data-automation-id="promptIcon"], [data-automation-id*="prompt" i][role="button"], [data-automation-id*="Icon" i]');
              if (!ic) return null; const r = ic.getBoundingClientRect(); return r.width > 2 ? { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) } : null; }""", input_ref)
            if isinstance(icon, dict):
                await ex.click_xy(int(icon["x"]), int(icon["y"]))
                await asyncio.sleep(0.8)
            await ex.eval("""(ref) => { const el = document.querySelector('[data-goat-ref="' + ref + '"]'); if (el) el.focus(); }""", input_ref)
            await asyncio.sleep(0.2)
        if not await _select_all_text(ex):
            return False, "the prompt's text box did not take focus"
        await ex.page.keyboard.press("Backspace")
        await ex.type_text(text[:80], real_keys=True, delay_ms=15)
        await asyncio.sleep(0.8)
        pre = await ex.eval(js.JS_LIST_OPTIONS)      # whatever is listed before the search is stale, not a result
        pre = pre if isinstance(pre, list) else []
        baseline_keys = set(baseline_keys) | {(o.get("text"), o.get("x"), o.get("y")) for o in pre}
        pre_texts = {str(o.get("text", "")).strip() for o in pre}
        await ex.page.keyboard.press("Enter")
    except Exception as e:
        return False, f"{type(e).__name__}"

    async def chips_now() -> list[str]:
        try:
            st = await ex.eval(js.JS_FIELD_COMMIT, ref)
            return [c for c in (st.get("chips") or []) if c] if isinstance(st, dict) else []
        except Exception:
            return []

    # The search runs on the server: "No Items." / an empty list is the transient state. Poll until results or
    # a chip appear (up to ~7 s).
    # results count only when their TEXTS differ from what was listed before the search (a stale list from an
    # earlier term re-renders at new positions but says the same things)
    listed: list = []
    for _ in range(14):
        await asyncio.sleep(0.5)
        chips_probe = await chips_now()
        if [c for c in chips_probe if c not in chips_before]:
            break
        got = await ex.eval(js.JS_LIST_OPTIONS)
        got = [o for o in (got if isinstance(got, list) else []) if o.get("text")]
        fresh = [o for o in got if str(o.get("text", "")).strip() not in pre_texts and str(o.get("text", "")).strip().lower() not in ("no items.", "no items", "")]
        if fresh:
            listed = fresh
            break
    chips = await chips_now()
    new_chips = [c for c in chips if c not in chips_before]
    for c in new_chips:
        if _chip_matches(c, wants):
            return True, c
    if new_chips:
        await _remove_wrong_chips(ex, ref, keep=chips_before)
    listed = [o for o in listed if o.get("text") not in chips]
    pick = next((o for o in listed if _chip_matches(o.get("text", ""), wants)), None)
    if pick is None:
        cand = best_option(listed, [w for w in wants if w])
        pick = cand if cand is not None and _chip_matches(cand["text"], wants) else None
    if pick is not None:
        try:
            pos = await ex.eval(js.JS_SCROLL_OPTION, pick["text"])
            if isinstance(pos, dict) and pos.get("ok"):
                await asyncio.sleep(0.3)
                pos2 = await ex.eval(js.JS_SCROLL_OPTION, pick["text"])
                pos = pos2 if isinstance(pos2, dict) and pos2.get("ok") else pos
                await ex.click_xy(int(pos["x"]), int(pos["y"]))
            else:
                await ex.click_xy(int(pick["x"]), int(pick["y"]))
            await asyncio.sleep(1.2)
        except Exception as e:
            return False, f"click on {pick['text']!r} failed: {type(e).__name__}"
        for attempt in range(2):
            for c in await chips_now():
                if c not in chips_before and _chip_matches(c, [pick["text"], *wants]):
                    return True, c
            if attempt == 0:
                # an expanded prompt shows its chips only once the popup closes; read too early, a good pick looked
                # missing and was removed again (Nissan's How Did You Hear, 2026-10-19)
                try:
                    await ex.page.keyboard.press("Escape")
                except Exception:
                    pass
                await asyncio.sleep(0.8)
        await _remove_wrong_chips(ex, ref, keep=chips_before)
    try:
        await ex.page.keyboard.press("Escape")
    except Exception:
        pass
    seen = [o["text"] for o in listed[:8]]
    return False, (f"the prompt's search for {text!r} returned {seen}; none matches the wanted wordings" if seen
                   else f"the prompt's search for {text!r} returned nothing usable" + (f" (it took {chips[:2]!r}, removed)" if chips else ""))


# An institution's campus or branch after a dash ("Texas A&M University - Commerce") is a different school unless one of
# the wanted variants names it ("- College Station"): a prefix match must never land on another campus (2026-10-18).
_INSTITUTION = re.compile(r"\b(university|college|institute|school|academy|polytechnic)\b|\ba\s*&\s*m\b", re.I)
_DASH_QUALIFIER = re.compile(r"\s[-\u2013\u2014]\s|(?<=\S)[-\u2013\u2014](?=[A-Z])")


def _foreign_qualifier(text: str, wants: list[str]) -> bool:
    parts = _DASH_QUALIFIER.split((text or "").strip(), maxsplit=1)
    if len(parts) < 2:
        return False
    extra = set(_norm(parts[1]).split())
    named = {w for x in wants for w in _norm(x).split()}
    return bool(extra) and not extra <= named


# A numbered option list ("1. Protected Veteran", "2. Not a Protected Veteran" on Oracle, 2026-10-19)
ENUM_PREFIX = re.compile(r"^\s*(\d{1,2}|[a-zA-Z])[.)]\s+")


def best_option(options: list[dict], wants: list[str]) -> dict | None:
    """best_option_core, then once more with each option's leading numbering ("2. ") set aside; the option returned
    is always the one the page listed. A list's own empty entry ('No Selection', 'Select One') is never an answer: L3Harris'
    SuccessFactors list took 'No' for 'No Selection' twice (2026-10-21)."""
    if not any(PLACEHOLDER.match(str(w or "").strip()) for w in wants if w):
        real = [o for o in options if not PLACEHOLDER.match(str(o.get("text") or "").strip())]
        if real and len(real) < len(options):
            options = real
    hit = _best_option_core(options, wants)
    if hit is not None or not any(ENUM_PREFIX.match(str(o.get("text") or "")) for o in options):
        return hit
    bare = [{**o, "text": ENUM_PREFIX.sub("", str(o.get("text") or ""), count=1), "_i": i} for i, o in enumerate(options)]
    hit = _best_option_core(bare, wants)
    return options[hit["_i"]] if hit is not None else None


# A GPA answer ("4.0 or above", "4.0+", "4.0") against numeric buckets: the bucket that holds the number wins, the
# highest one when two hold it ("3.75 - 4" and "4+" both hold 4.0). Klaviyo (2026-10-19) listed '3.5 - 3.74',
# '3.75 - 4', '4+': no stored wording matched, and "3.5+" (its '+' lost in normalising) prefix-matched '3.5 - 3.74'.
_GPA_WANT = re.compile(r"^\s*([0-4]\.\d{1,2})\s*(\+|or above|and above|or higher|and higher|or more|or greater)?\s*$", re.I)
_NUM = r"(\d(?:\.\d{1,2})?)"
_RANGE = re.compile(_NUM + r"\s*(?:-|\u2013|\u2014|to)\s*" + _NUM)
_AT_LEAST = re.compile(_NUM + r"\s*(?:\+|or above|and above|or higher|and higher|or more|or greater)|"
                       r"(?:above|over|at least|greater than|>=?)\s*" + _NUM, re.I)
_BELOW = re.compile(r"(?:below|under|less than|<)\s*" + _NUM + r"|" + _NUM + r"\s*(?:or below|or under|or less|and below|or lower)", re.I)


def _gpa_bucket(text: str) -> tuple[float, float, bool] | None:
    """(low, high, high_inclusive) for a GPA bucket label, else None."""
    t = (text or "").strip()
    m = _RANGE.search(t)
    if m:
        a, b = sorted((float(m.group(1)), float(m.group(2))))
        return (a, b, True) if b <= 5 else None
    m = _AT_LEAST.search(t)
    if m:
        v = float(m.group(1) or m.group(2))
        return (v, 5.0, True) if v <= 5 else None
    m = _BELOW.search(t)
    if m:
        v = float(m.group(1) or m.group(2))
        inclusive = bool(m.group(2))
        return (0.0, v, inclusive) if v <= 5 else None
    m = re.fullmatch(_NUM, t)
    if m:
        v = float(m.group(1))
        return (v, v, True) if v <= 5 else None
    return None


def gpa_option(options: list[dict], wants: list[str]) -> dict | None:
    m = _GPA_WANT.match((wants or [""])[0] or "")
    if not m:
        return None
    gpa = float(m.group(1))
    parsed = [(o, _gpa_bucket(o.get("text") or "")) for o in options]
    parsed = [(o, b) for o, b in parsed if b is not None]
    if len(parsed) < 2:
        return None
    hold = [(o, b) for o, b in parsed if b[0] <= gpa and (gpa <= b[1] if b[2] else gpa < b[1])]
    if not hold:
        return None
    return max(hold, key=lambda ob: ob[1][0])[0]


# A self-identification chain starts with 'decline' (Surya: decline whenever the form offers it). Forms word the decline
# their own way; GDIT's 'I do not wish to disclose (United States of America)' matched none of the stored wordings and
# the chain fell through to his race (2026-10-20).
DECLINE_OPT = re.compile(
    r"\b(decline[sd]?|do not wish|don.?t wish|prefer not|choose not|chose not|elect not|opt not|"
    r"not (wish|want) to|rather not|not to (disclose|answer|say|identify|self[- ]?identify|respond|specify|provide)|"
    r"undisclosed|not (specified|declared|disclosed|provided))\b", re.I)


def decline_option(options: list[dict], wants: list[str]) -> dict | None:
    if not wants or _norm(wants[0]) != "decline":
        return None
    dec = [o for o in options if DECLINE_OPT.search(str(o.get("text") or ""))]
    return dec[0] if dec else None


# A pay answer (his stored 41600 a year) against dollar ranges: the narrowest range that holds it ("$40,000-$60,000"
# on CACI's form, 2026-10-20); an hourly list (bounds under $1,000) is read against $20 an hour.
_MONEY_NUM = r"\$?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kK]\b)?"


def _money_value(num: str, k: str | None) -> float:
    v = float(num.replace(",", ""))
    return v * 1000 if k else v


def _money_bucket(text: str) -> tuple[float, float] | None:
    t = text or ""
    if "$" not in t and not re.search(r"\d\s*[kK]\b", t):
        return None
    m = re.search(_MONEY_NUM + r"\s*(?:-|\u2013|\u2014|to)\s*" + _MONEY_NUM, t)
    if m:
        a, b = _money_value(m.group(1), m.group(2)), _money_value(m.group(3), m.group(4))
        return (min(a, b), max(a, b))
    m = re.search(r"(?:up to|less than|under|below|<)\s*" + _MONEY_NUM, t, re.I)
    if m:
        return (0.0, _money_value(m.group(1), m.group(2)))
    m = re.search(_MONEY_NUM + r"\s*(?:\+|or (?:more|above|higher)|and (?:above|up))", t, re.I)
    if m:
        return (_money_value(m.group(1), m.group(2)), float("inf"))
    m = re.search(r"(?:more than|over|above|greater than|>)\s*" + _MONEY_NUM, t, re.I)
    if m:
        return (_money_value(m.group(1), m.group(2)), float("inf"))
    return None


def money_option(options: list[dict], wants: list[str]) -> dict | None:
    m = re.fullmatch(r"\s*\$?\s*(\d{1,3}(?:,\d{3})+|\d{4,7})(?:\.\d+)?\s*", (wants or [""])[0] or "")
    if not m:
        return None
    annual = float(m.group(1).replace(",", ""))
    if annual < 1000:
        return None
    parsed = [(o, _money_bucket(str(o.get("text") or ""))) for o in options]
    parsed = [(o, b) for o, b in parsed if b is not None]
    if len(parsed) < 2:
        return None
    targets = [annual]
    if all(b[1] < 1000 or b[1] == float("inf") and b[0] < 1000 for _o, b in parsed):
        targets = [round(annual / 2080, 2)]           # an hourly list
    for target in targets:
        hold = [(o, b) for o, b in parsed if b[0] <= target <= b[1]]
        if hold:
            return min(hold, key=lambda ob: (ob[1][1] - ob[1][0]))[0]
    return None


_PROFICIENCY_SCALE = ["native", "bilingual", "mother tongue", "fluent", "expert", "superior", "distinguished", "advanced",
                      "proficient", "full professional", "professional", "upper intermediate", "intermediate", "conversational",
                      "working", "limited", "elementary", "basic", "beginner", "novice"]


def proficiency_option(options: list[dict], wants: list[str]) -> dict | None:
    """The highest language-proficiency level among the options, for his native-English chain only."""
    if not wants or _norm(wants[0]) not in ("native", "native or bilingual"):
        return None
    best, rank = None, len(_PROFICIENCY_SCALE)
    for o in options:
        t = _norm(o.get("text"))
        for i, w in enumerate(_PROFICIENCY_SCALE):
            if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", t):
                if i < rank:
                    best, rank = o, i
                break
    return best


# His degree is a Bachelor of Science. A bare level answer ('Bachelors', "Bachelor's") prefix-matched Itron's 'Bachelors of
# Business Administration (BBA)', and the tool called that 'this portal's wording for Bachelor of Science' (2026-10-21).
_DEGREE_LEVEL_WANT = re.compile(r"^\s*(bachelor('?s)?( degree)?|undergraduate|bachelor'?s? of science( \(b\.?s\.?\))?|b\.?s\.?c?)\s*$", re.I)
_SCIENCE_BACHELOR = re.compile(r"\bbachelor'?s?\s+(degree\s+)?(of|in)\s+science\b|^\s*b\.?\s?sc?\.?\s*(\(|$)|\(\s*b\.?\s?sc?\.?\s*\)", re.I)
_OTHER_BACHELOR = re.compile(r"\bbachelor'?s?\s+(degree\s+)?(of|in)\s+(?!science\b)[a-z]", re.I)


def degree_option(options: list[dict], wants: list[str]) -> tuple[dict | None, list[dict]]:
    """For a bachelor-level answer: the science bachelor's the list offers, else the options without the bachelor's
    degrees of other fields (which only an exact answer may pick)."""
    if not any(_DEGREE_LEVEL_WANT.match(w or "") for w in wants):
        return None, options
    exact = {_norm(w) for w in wants if w}
    sci = [o for o in options if _SCIENCE_BACHELOR.search(o.get("text") or "")]
    if sci:
        return sci[0], options
    return None, [o for o in options if _norm(o.get("text")) in exact or not _OTHER_BACHELOR.search(o.get("text") or "")]


def _best_option_core(options: list[dict], wants: list[str]) -> dict | None:
    """Exact match first. A prefix/contains match is accepted only when it is UNambiguous: if several
    options share the prefix (Texas A&M University - College Station / - Commerce / - Kingsville) the
    caller must name the exact one. Picking the shortest would silently choose the wrong campus."""
    seen: set[str] = set()
    options = [o for o in options if not (_norm(o.get("text")) in seen or seen.add(_norm(o.get("text"))))]
    dec = decline_option(options, wants)
    if dec is not None:
        return dec
    gpa_hit = gpa_option(options, wants)
    if gpa_hit is not None:
        return gpa_hit
    pay_hit = money_option(options, wants)
    if pay_hit is not None:
        return pay_hit
    deg_hit, options = degree_option(options, wants)
    if deg_hit is not None:
        return deg_hit
    institution = any(_INSTITUTION.search(w or "") for w in wants)
    for want in wants:
        w = _norm(want)
        if not w:
            continue
        if re.fullmatch(r"\s*\d+(\.\d+)?\s*\+\s*", want or ""):
            # "3.5+" normalises to "3 5", which would prefix-match "3.5 - 3.74": a number-plus answer matches only exactly
            exact = [o for o in options if (o.get("text") or "").replace(" ", "") == want.replace(" ", "")]
            if exact:
                return exact[0]
            continue
        exact = [o for o in options if _norm(o["text"]) == w]
        if exact:
            return exact[0]
        # the name line of a multi-line option ("Texas A&M University / United States / tamu.edu" on Ashby, where
        # "Texas A&M University System" also starts with the want, ibotta 2026-10-19)
        exact = [o for o in options if o.get("first") and o.get("first") != o.get("text") and _norm(o["first"]) == w]
        if len(exact) == 1:
            return exact[0]
        # a prefix or contains match on a school never lands on a campus the wants do not name
        near = [o for o in options if not (institution and _foreign_qualifier(o["text"], wants))]
        starts = [o for o in near if _norm(o["text"]) == w or _norm(o["text"]).startswith(w + " ")]
        if len(starts) == 1:
            return starts[0]
        if len(starts) > 1:
            # Allowed only when the extra text is a parenthetical/qualifier of the same thing, e.g. "Yes (I am)".
            # (on the raw text: _norm strips the parentheses this rule looks for)
            bare = [o for o in starts if re.fullmatch(re.escape(want.strip().lower()) + r"\s*(\(.*\))?", (o["text"] or "").strip().lower())]
            if len(bare) == 1:
                return bare[0]
            continue
        # whole words only: 'no' must never match 'unknown', 'none' or 'not applicable'
        has = [o for o in near if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", _norm(o["text"]))]
        if institution:
            # words in FRONT of a school's name make it another school: "West Texas A&M University" contains "Texas A&M
            # University" and was picked on Coinbase's and ID.me's Greenhouse forms (2026-10-19)
            has = [o for o in has if _norm(o["text"]).startswith(w)]
        if len(has) == 1:
            return has[0]
        # "Bachelor's" vs "Bachelors", "E-mail" vs "Email": equal once spaces and punctuation are gone.
        sq = [o for o in options if _squash(o["text"]) == _squash(want)]
        if len(sq) == 1:
            return sq[0]
    # A month-year answer against quarter ranges ("April - June 2028" holds May 2028; Oracle graduation dates, 2026-10-19).
    for want in wants:
        m = _MONTH_YEAR.match((want or "").strip())
        if not m:
            continue
        mon = (m.group("mon") or "").lower()
        num = next((i + 1 for i, n in enumerate(_MONTH_NAMES) if mon and n.lower().startswith(mon[:3])), 0) if mon else int(m.group("mm") or m.group("mm2") or 0)
        year = m.group("y1") or m.group("y2") or m.group("y3")
        hits = []
        for o in options:
            r = re.search(r"([A-Za-z]{3,9})\.?\s*[-–—to]+\s*([A-Za-z]{3,9})\.?,?\s+((?:19|20)\d{2})", o.get("text") or "")
            if not r or r.group(3) != year:
                continue
            a = next((i + 1 for i, n in enumerate(_MONTH_NAMES) if n.lower().startswith(r.group(1).lower()[:3])), 0)
            b = next((i + 1 for i, n in enumerate(_MONTH_NAMES) if n.lower().startswith(r.group(2).lower()[:3])), 0)
            if a and b and a <= num <= b:
                hits.append(o)
        if len({_norm(h.get("text")) for h in hits}) == 1:
            return hits[0]
    # His English is native: when none of his wordings is offered, the highest level the form lists is his (Ecolab's
    # scale topped out at 'Proficient', 2026-10-20).
    prof = proficiency_option(options, wants)
    if prof is not None:
        return prof
    # A Yes/No answer against consent-style options ("I agree to receive SMS...", "Please do not send...").
    for want in wants:
        w = _norm(want)
        if w in ("yes", "true", "agree", "i agree", "i consent", "accept"):
            aff = [o for o in options if AFFIRM_OPT.match((o["text"] or "").strip()) and not NEG_OPT.match((o["text"] or "").strip())]
            if len(aff) == 1:
                return aff[0]
        if w in ("no", "false"):
            neg = [o for o in options if NEG_OPT.match((o["text"] or "").strip())]
            if len(neg) == 1:
                return neg[0]
    return None


_MONTH_YEAR = re.compile(r"^(?:(?P<mon>[A-Za-z]{3,9})\.?,?\s+(?P<y1>(?:19|20)\d{2})|(?P<mm>\d{1,2})[/-](?:\d{1,2}[/-])?(?P<y2>(?:19|20)\d{2})"
                         r"|(?P<y3>(?:19|20)\d{2})-(?P<mm2>\d{1,2})(?:-\d{1,2})?)$")
_MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                "November", "December"]


def date_wants(wants: list[str]) -> list[str]:
    """For each want that is a month and year ('August 2025', '08/2025', '2025-08'): its month name, the month's
    three-letter form and the year, for pickers that list months or years alone (Epic Games' 'Start Date (Month)',
    2026-10-19). Only date-shaped wants count: 'Marketing' is not March."""
    extra: list[str] = []
    for w in wants:
        m = _MONTH_YEAR.match((w or "").strip())
        if not m:
            continue
        num = 0
        if m.group("mon"):
            mon = m.group("mon").lower()
            num = next((i + 1 for i, n in enumerate(_MONTH_NAMES) if n.lower().startswith(mon[:3]) and n.lower().startswith(mon)), 0)
        else:
            num = int(m.group("mm") or m.group("mm2") or 0)
        year = m.group("y1") or m.group("y2") or m.group("y3")
        if not 1 <= num <= 12:
            continue
        for x in (_MONTH_NAMES[num - 1], _MONTH_NAMES[num - 1][:3], year):
            if x and x not in wants and x not in extra:
                extra.append(x)
    return extra


def month_numbers(wants: list[str]) -> list[str]:
    """'08' and '8' for each month-year want: only for a field labelled as a month (a Day list would take '8' too)."""
    out: list[str] = []
    for w in date_wants(list(wants)):
        if w in _MONTH_NAMES:
            n = _MONTH_NAMES.index(w) + 1
            for x in (f"{n:02d}", str(n)):
                if x not in out:
                    out.append(x)
    return out


async def choose(ex: Executor, ref: str, wants: list[str], *, type_to_filter: bool = True) -> FieldResult:
    extra = date_wants(list(wants))
    if extra:
        # Epic Games' 'Start Date (Month)' lists 01-12, not names (2026-10-19)
        try:
            info = await ex.read_ref(ref)
            if re.search(r"\bmonth\b", str(info.get("label") or ""), re.I):
                extra += [x for x in month_numbers(list(wants)) if x not in extra]
        except Exception:
            pass
    wants = list(wants) + [x for x in extra if x not in wants]
    r = await _choose(ex, ref, wants, type_to_filter=type_to_filter)
    if autosaves(ex):
        await ex.wait_saves()
    if r.ok and NO_RESULTS.match(r.readback or ""):
        # the list's empty-search message is not the field's value (Oracle's Veteran Status read back "No results were
        # found." over a correct pick, and the gate blocked every submit, American Express 2026-10-19): read the field
        try:
            await ex.page.keyboard.press("Escape")
            info = await ex.read_ref(ref)
            shown = str(info.get("shown") or info.get("value") or "").strip()
        except Exception:
            shown = ""
        if shown and not NO_RESULTS.match(shown):
            r = FieldResult(r.ref, _chip_matches(shown, wants) or best_option([{"text": shown}], wants) is not None, shown, r.error)
        else:
            r = FieldResult(r.ref, False, "", "the picker showed 'no results' and the field holds nothing: choose again with a shorter wording")
    return r


NO_RESULTS = re.compile(r"^\s*(no (results|matches|matching (results|options|items))|no options|no items)( were| was)?( found)?\.?\s*$", re.I)


async def _settled_options(ex: Executor, baseline_keys: set, key, wants: list[str], polls: int = 12) -> list[dict]:
    """The options a typed search shows once it has answered: the first list holding a wanted option, else the list
    once it has stayed the same for two polls in a row (0.5 s apart), else whatever is showing at the end."""
    filtered: list[dict] = []
    prev, stable = None, 0
    for _ in range(polls):
        await asyncio.sleep(0.5)
        got = await ex.eval(js.JS_LIST_OPTIONS)
        filtered = [o for o in (got if isinstance(got, list) else []) if key(o) not in baseline_keys]
        if filtered and best_option(filtered, [w for w in wants if w]) is not None:
            break
        texts = tuple(str(o.get("text")) for o in filtered)
        stable = stable + 1 if (filtered and texts == prev) else 0
        prev = texts
        if stable >= 2:
            break
    return filtered


# SmartRecruiters' pickers (<spl-autocomplete>): the input sits two shadow roots down and the options are
# <spl-select-option>s in the host's own shadow root, so the generic list read found nothing and choose said "the picker
# for this field did not open" (CapTech's graduation term and Gender, 2026-10-21).
JS_SPL_HOST = js._COMMON_WRAP("""
  const el = findRef(ref);
  if (!el) return null;
  let n = el;
  for (let i = 0; i < 60 && n; i++) {
    if (n.tagName === 'SPL-AUTOCOMPLETE' || n.tagName === 'SPL-SELECT') return { id: n.id || '', tag: n.tagName.toLowerCase() };
    const r = n.getRootNode ? n.getRootNode() : null;
    n = n.parentElement || (r && r.host) || null;
  }
  return null;
""", "ref")
JS_SPL_OPTIONS = js._COMMON_WRAP("""
  const [id, tag] = args;
  let h = null;
  for (const r of allRoots(document)) { const x = r.querySelector(tag + '[id="' + id + '"]'); if (x) { h = x; break; } }
  if (!h || !h.shadowRoot) return [];
  return Array.from(h.shadowRoot.querySelectorAll('spl-select-option')).map((o, idx) => {
    const b = o.getBoundingClientRect();
    return { idx, text: (o.innerText || o.textContent || '').replace(/[ \\t\\r\\n]+/g, ' ').trim(), w: b.width, h: b.height };
  }).filter(o => o.w > 0 && o.h > 0 && o.text);
""", "args")


async def _choose_spl(ex: Executor, ref: str, wants: list[str]) -> FieldResult | None:
    """Pick in a SmartRecruiters <spl-autocomplete>: open it with a real click, read its own options, click the first
    wanted one that is offered (our preference order), read the box back. None when the control is not one."""
    try:
        host = await ex.eval(JS_SPL_HOST, ref)
    except Exception:
        host = None
    if not isinstance(host, dict) or not host.get("id"):
        return None
    hid, tag = str(host["id"]), str(host.get("tag") or "spl-autocomplete")
    box = ex.page.locator(f'[data-goat-ref="{ref}"]').first
    opts: list[dict] = []
    for attempt in range(2):
        try:
            await box.click(timeout=5000)
        except Exception:
            try:
                await box.focus(timeout=3000)
            except Exception:
                pass
        for _ in range(12):
            await asyncio.sleep(0.25)
            got = await ex.eval(JS_SPL_OPTIONS, [hid, tag])
            opts = got if isinstance(got, list) else []
            if opts:
                break
        if opts:
            break
    if not opts:
        await ex.page.keyboard.press("Escape")
        return FieldResult(ref, False, "", "the SmartRecruiters list showed no options")
    pick = best_option([{"text": o["text"], "_idx": o["idx"]} for o in opts], [w for w in wants if w])
    if pick is None:
        await ex.page.keyboard.press("Escape")
        return FieldResult(ref, False, "", f"no option matched {wants[:3]}; the list offers: {[o['text'] for o in opts][:12]}")
    idx = next((o["idx"] for o in opts if o["text"] == pick.get("text")), pick.get("_idx"))
    await ex.page.locator(f'{tag}[id="{hid}"] spl-select-option').nth(int(idx)).click(timeout=5000)
    shown = ""
    for _ in range(12):
        await asyncio.sleep(0.2)
        info = await ex.read_ref(ref)
        shown = str(info.get("value") or info.get("shown") or "").strip()
        if shown:
            break
    ok = bool(shown) and (_norm(shown) == _norm(pick["text"]) or best_option([{"text": shown}], [pick["text"]]) is not None)
    return FieldResult(ref, ok, shown, "" if ok else f"picked '{pick['text']}' but the box shows '{shown}'")


async def _choose(ex: Executor, ref: str, wants: list[str], *, type_to_filter: bool = True) -> FieldResult:
    """Pick an option in a custom dropdown / combobox / autocomplete.

    Order: (1) react-select props when the control is one; (2) click to open, optionally type the
    first letters with real keys, read the visible options, click the best match; (3) keyboard
    fallback (type + Enter is NOT used blindly: Enter can submit a form). Verified by read-back."""
    info = await ex.read_ref(ref)
    if not info.get("found"):
        return FieldResult(ref, False, "", "ref not found")
    before = (info.get("value") or info.get("shown") or "")
    spl = await _choose_spl(ex, ref, wants)
    if spl is not None:
        return spl
    if info.get("tag") == "select":
        # A native <select> has no DOM list to click (Lever's custom questions, 2026-10-18): pick by its options.
        native = await ex.eval(js.JS_FIELD_CHOICES, ref)
        opts = [{"text": o} for o in (native if isinstance(native, list) else [])]
        pick = best_option(opts, [w for w in wants if w]) if opts else None
        if pick is None:
            return FieldResult(ref, False, before, f"no option matched {wants[:3]}; the select offers: {[o['text'] for o in opts][:12]}")
        r = (await fill_batch(ex, [{"ref": ref, "value": pick["text"]}]))[0]
        return FieldResult(ref, r.ok, r.readback or pick["text"], r.error)
    # Workday multi-select trees (How Did You Hear About Us?) count their picks ("0 items selected") and nest
    # options under categories: the prompt's search box filters the whole tree, so each want is typed and
    # matched exactly instead of drilling into a category by hand.
    multi_tree = bool(re.search(r"\bitems? selected\b", before or "", re.I))
    try:
        wd_prompt = bool(await ex.eval(JS_IS_WORKDAY_PROMPT, ref))
    except Exception:
        wd_prompt = False
    # 0. already showing the wanted option: nothing to click.
    for w in wants:
        if _norm(w) and not PLACEHOLDER.match(before or "") and _norm(before) == _norm(w):
            return FieldResult(ref, True, before)

    # 1. react-select through its own props: list its options, pick by OUR preference order (the first
    #    want that is offered wins, never the first option in page order), then select that exact label.
    listed = await ex.eval(js.JS_REACT_SELECT, [ref, None])
    if isinstance(listed, dict) and listed.get("list") and listed.get("options"):
        pick = best_option([{"text": t} for t in listed["options"]], [w for w in wants if w])
        if pick is not None:
            rs = await ex.eval(js.JS_REACT_SELECT, [ref, "=" + pick["text"]])
            if isinstance(rs, dict) and rs.get("ok"):
                await asyncio.sleep(0.3)
                after = await ex.read_ref(ref)
                shown = after.get("shown") or after.get("value") or rs.get("selected", "")
                return FieldResult(ref, True, str(rs.get("selected", shown)))

    # 2. open with a real click, type, read the options that APPEARED (not lists elsewhere on the page),
    #    click the match; walk multi-level prompts (Workday "Job Board" -> "LinkedIn").
    def _key(o: dict) -> tuple:
        return (o.get("text"), o.get("x"), o.get("y"))

    # A list left open by an earlier call (Workday's 'Select One' menus ignore Escape) would be counted as
    # baseline, so this control's own options would look like "another control's" and a click on the control
    # could land on the overlay instead (Booz Allen, 2026-10-18): close it before measuring the baseline.
    await _close_open_lists(ex)
    baseline = await ex.eval(js.JS_LIST_OPTIONS)
    baseline_keys = {_key(o) for o in (baseline if isinstance(baseline, list) else [])}
    open_now = False
    toggled = False
    last_err = "the picker did not open"
    for attempt, want in enumerate(wants[:6] or [""]):
        if not open_now:
            try:
                await ex.page.keyboard.press("Escape")
            except Exception:
                pass
            try:
                await ex.click_ref(ref)
            except ClickRefused as e:
                return FieldResult(ref, False, before, str(e))
            # Read what the click opened first: Workday's button dropdowns ("Select One") list every option
            # and CLOSE when typed into, so type to filter only when no wanted option is already showing.
            # Lists fetched from the network (Greenhouse school / graduation / honor pickers) can take a
            # couple of seconds to appear: poll instead of a single 0.6 s wait.
            shown_now = []
            for _ in range(8):
                await asyncio.sleep(0.3)
                shown_now = await ex.eval(js.JS_LIST_OPTIONS)
                shown_now = [o for o in (shown_now if isinstance(shown_now, list) else []) if _key(o) not in baseline_keys]
                if shown_now:
                    break
            # A Workday prompt whose top level does not show the first preference may hold it inside a category: its
            # search box finds it there. Ecolab (2026-10-20) listed no 'LinkedIn' at the top and the chain settled for
            # 'Employment Agency / Search Firm', its third choice. Search for the first preference before settling.
            if wd_prompt and attempt == 0 and wants and wants[0] and shown_now \
                    and best_option(shown_now, [wants[0]]) is None and best_option(shown_now, wants) is not None:
                ok_s, shown_s = await _type_and_enter(ex, ref, [wants[0]], baseline_keys)
                if ok_s:
                    return FieldResult(ref, True, shown_s)
                try:
                    await ex.page.keyboard.press("Escape")
                    await ex.click_ref(ref)
                except Exception:
                    pass
                shown_now = []
                for _ in range(8):
                    await asyncio.sleep(0.3)
                    shown_now = await ex.eval(js.JS_LIST_OPTIONS)
                    shown_now = [o for o in (shown_now if isinstance(shown_now, list) else []) if _key(o) not in baseline_keys]
                    if shown_now:
                        break
            if type_to_filter and want and not (shown_now and best_option(shown_now, [want] if multi_tree else wants)) \
                    and await _text_box_focused(ex):
                await _select_all_text(ex)     # replace, never append to, text left in the search box
                # Oracle's search-as-you-type boxes drop keys typed 20 ms apart ('6611 Cadogan Court' searched as
                # '6611 O...', AutoZone 2026-10-19): type them at 45 ms.
                slow = "oraclecloud.com" in (ex.url or "")
                await ex.type_text(want[:40], real_keys=True, delay_ms=45 if slow else 20)
                # Server-backed autocompletes (Ashby's university search, Sierra 2026-10-18) answer in a few seconds
                # A search-as-you-type list first shows results for the start of the query: wait for a match or a list
                # that stopped changing (Oracle's address search listed other '6611 ...' streets before '6611 CADOGAN
                # COURT, SUGAR LAND, TEXAS, 77479' came back; AutoZone and American Express, 2026-10-19).
                filtered = await _settled_options(ex, baseline_keys, _key, wants, polls=12)
                if not filtered or best_option(filtered, wants) is None:
                    # A search that matches the whole query as one substring finds nothing for a long name (Sierra's
                    # curated Ashby list has "Texas A&M": "Texas A&M University" returns No results, "Texas" finds it,
                    # 2026-10-19): retry with the first two words, then the first word, and match against every want.
                    # Also when the list opened on its first page of entries (Epic Games' school search shows the
                    # first 100 schools, A to Z, until a query returns matches, 2026-10-19).
                    words = re.findall(r"\S+", want)
                    for short in [" ".join(words[:2]), words[0] if words else ""]:
                        if not short or short == want or len(short) < 4:
                            continue
                        await _select_all_text(ex)
                        await ex.type_text(short[:40], real_keys=True, delay_ms=20)
                        filtered = await _settled_options(ex, baseline_keys, _key, wants, polls=10)
                        if filtered and best_option(filtered, wants):
                            break
                if not filtered and shown_now:
                    # The typed wording filtered a react-select down to nothing (Lyft's 'Work Authorization' lists
                    # sentences, the want was 'Yes'): clear it so the whole list is back for the yes/no mapping.
                    await _select_all_text(ex)
                    await ex.page.keyboard.press("Backspace")
                    await asyncio.sleep(0.6)
        options = await ex.eval(js.JS_LIST_OPTIONS)
        options = options if isinstance(options, list) else []
        new_opts = [o for o in options if _key(o) not in baseline_keys]
        if not new_opts and not toggled:
            # Some comboboxes open only from their own button (Ashby's "Open university dropdown", Sierra 2026-10-18):
            # click it once and read the list it shows for the text already typed.
            toggled = True
            tog = await ex.eval(js.JS_OPEN_TOGGLE, ref)
            if isinstance(tog, dict) and tog.get("ok"):
                try:
                    await ex.click_xy(int(tog["x"]), int(tog["y"]))
                except ClickRefused:
                    tog = None
                for _ in range(10 if tog else 0):
                    await asyncio.sleep(0.4)
                    opts2 = await ex.eval(js.JS_LIST_OPTIONS)
                    new_opts = [o for o in (opts2 if isinstance(opts2, list) else []) if _key(o) not in baseline_keys]
                    if new_opts:
                        break
                if new_opts:
                    options = new_opts
        if not new_opts:
            # Options may be on screen, but if none of them are NEW they belong to some other control
            # (a phone-country prompt, a menu left open). Never match this field against those.
            last_err = (f"the picker for this field did not open"
                        + (f"; the options on screen ({[o['text'] for o in options[:4]]}) belong to another control"
                           if options else ""))
            open_now = False
            continue
        options = new_opts
        pick = best_option(options, [want] if want else wants)
        if pick is None and open_now:
            # second level of a multi-level prompt (Job Board -> LinkedIn): try the whole preference list
            pick = best_option(options, wants)
        if pick is None and not open_now:
            # the list shows every option (no type-to-filter effect): take the most preferred one present
            pick = best_option(options, wants)
        if pick is None and open_now and type_to_filter and await _text_box_focused(ex):
            # A category's inner list is virtualized (Workday "Job Board" -> Career Builder ... with LinkedIn
            # below the fold): type each preference into the prompt's search box and read what it filters to.
            for w in [x for x in wants if _norm(x)][:4]:
                await _select_all_text(ex)
                await ex.type_text(w[:40], real_keys=True, delay_ms=20)
                await asyncio.sleep(1.2)
                filtered = await ex.eval(js.JS_LIST_OPTIONS)
                filtered = [o for o in (filtered if isinstance(filtered, list) else []) if _key(o) not in baseline_keys]
                pick = best_option(filtered, [w])
                if pick is not None:
                    options = filtered
                    break
        open_now = False
        if pick is None and options and any(_norm(o["text"]).startswith(_norm(want)) for o in options):
            # Ambiguous prefix: report the candidates so the model names the exact one.
            sample = [o["text"] for o in options if _norm(o["text"]).startswith(_norm(want))][:12]
            try:
                await ex.page.keyboard.press("Escape")
            except Exception:
                pass
            return FieldResult(ref, False, before, f"ambiguous: several options start with {want!r}; name the exact one: {sample}")
        if pick:
            try:
                pos = await ex.eval(js.JS_SCROLL_OPTION, pick["text"])
                if isinstance(pos, dict) and pos.get("ok"):
                    await asyncio.sleep(0.4)                       # a virtualized list re-renders after the scroll
                    pos2 = await ex.eval(js.JS_SCROLL_OPTION, pick["text"])
                    pos = pos2 if isinstance(pos2, dict) and pos2.get("ok") else pos
                    pick = {**pick, "x": pos["x"], "y": pos["y"]}
                    await asyncio.sleep(0.15)
            except Exception:
                pass
            try:
                await ex.click_xy(int(pick["x"]), int(pick["y"]))
            except ClickRefused as e:
                return FieldResult(ref, False, before, str(e))
            await asyncio.sleep(0.6)
            if wd_prompt:
                # A Workday prompt holds its picks as chips; the text in its box is the search typed into it. Gordon
                # Food Service (2026-10-20): a click on the 'Job Board' category left the typed 'LinkedIn' in the box,
                # that text was read as the value, and the form was left with no pick at all.
                st = await ex.eval(js.JS_FIELD_COMMIT, ref)
                chips = [c for c in ((st.get("chips") or []) if isinstance(st, dict) else []) if c]
                hit = next((c for c in chips if _chip_matches(c, [pick["text"], *[w for w in wants if w]])), None)
                if hit:
                    return FieldResult(ref, True, hit)
                nxt = await ex.eval(js.JS_LIST_OPTIONS)
                nxt = [o for o in (nxt if isinstance(nxt, list) else []) if _key(o) not in baseline_keys]
                if nxt and {o.get("text") for o in nxt} != {o.get("text") for o in options}:
                    # a category opened its next level (Job Board -> Indeed / LinkedIn / ...): stay in it
                    open_now = True
                    last_err = f"inside {pick['text']!r} the list shows: {[o['text'] for o in nxt[:12]]}; none matched the preference list"
                    continue
                # a leaf that only showed its text: commit it through its own radio or the keyboard
                committed, why = await _force_commit(ex, ref, pick["text"], want)
                if committed:
                    return FieldResult(ref, True, pick["text"])
                last_err = f"clicked {pick['text'][:60]!r} but it did not commit ({why})"
                continue
            after = await ex.read_ref(ref)
            shown = (after.get("value") or after.get("shown") or "")
            if after.get("tag") == "input" and (not shown.strip() or PLACEHOLDER.match(shown.strip())):
                # SAP SuccessFactors' paginated selects (L3Harris, Westinghouse, 2026-10-20) show the pick, and clear their
                # 'required' error, only once the box loses focus: read while focused, the box said 'No Selection' and a
                # correct pick was reported failed. Leave the box, then read it again.
                try:
                    await ex.eval("() => { const a = document.activeElement; if (a && a !== document.body && a.blur) a.blur(); return true; }")
                    await asyncio.sleep(0.6)
                    after = await ex.read_ref(ref)
                    shown = (after.get("value") or after.get("shown") or "")
                except Exception:
                    pass
            if not shown.strip() or after.get("tag") == "button":
                # Ashby: the ref may be the toggle button beside the combobox input that actually holds the pick
                # (ibotta, 2026-10-18), or the autocomplete clears its input and shows the pick elsewhere. Read the
                # companion input, then the outline's reading of the control (what the gate will see).
                try:
                    comp = await ex.eval(JS_COMPANION_VALUE, ref)
                    cv = str((comp or {}).get("value") or "") if isinstance(comp, dict) else ""
                    # an input beside a button is the real control; a chip only fills an empty read-back
                    if cv.strip() and ((comp.get("from") == "input" and (not shown.strip() or after.get("tag") == "button")) or not shown.strip()):
                        shown = cv
                except Exception:
                    pass
            if not shown.strip():
                try:
                    from . import snapshot as _snapmod
                    item = (await _snapmod.take(ex)).refs.get(ref) or {}
                    shown = str(item.get("shown") or item.get("value") or "")
                except Exception:
                    pass
            shown = await text_not_internal_id(ex, ref, shown)
            # Workday multi-select trees ("How Did You Hear About Us?") count their picks and list them after the count.
            if re.search(r"\b[1-9]\d* items? selected\b", shown, re.I):
                if _norm(pick["text"]) in _norm(shown):
                    return FieldResult(ref, True, pick["text"])
                # a different row took the click (the list shifted): toggle it off and try the next want
                stray = re.sub(r".*?items? selected,?\s*", "", shown, flags=re.I).strip()
                stray_text = (stray.split("  ")[0] or stray).strip()
                try:
                    sp = await ex.eval(js.JS_SCROLL_OPTION, stray_text[: len(stray_text) // 2 + 1] if stray_text and stray_text == stray_text[: len(stray_text) // 2 + 1] * 2 else stray_text)
                    if isinstance(sp, dict) and sp.get("ok"):
                        await ex.click_xy(int(sp["x"]), int(sp["y"]))
                        await asyncio.sleep(0.5)
                except Exception:
                    pass
                last_err = f"the click landed on {stray_text[:40]!r} instead of {pick['text']!r} (the list shifted); it was toggled off"
                open_now = False
                continue
            if shown and not PLACEHOLDER.match(shown) and not LEVELISH.search(shown) and (_norm(pick["text"])[:6] in _norm(shown) or _norm(want)[:6] in _norm(shown)):
                # The text is in the box, but a Workday prompt also shows the typed search there: only a
                # selected-item chip means it committed.
                committed, why = await _committed(ex, ref, pick["text"])
                if not committed:
                    committed, why = await _force_commit(ex, ref, pick["text"], want)
                if not committed:
                    try:
                        await ex.page.keyboard.press("Escape")
                    except Exception:
                        pass
                    return FieldResult(ref, False, shown, f"the picker shows {shown[:60]!r} but did not commit it ({why}); "
                                                          "try the next option text, or the computer tool on the option itself")
                # Prefer the picked option's own text when the control echoes its label too.
                return FieldResult(ref, True, pick["text"] if _norm(pick["text"]) in _norm(shown) else shown)
            # the control may have been replaced; accept if the picked option text is now on the page near the field
            if _norm(pick["text"]) and _norm(pick["text"]) in _norm(after.get("shown", "")):
                return FieldResult(ref, True, pick["text"])          # the field text echoes the label; the pick is the value
            # still a placeholder: a multi-level prompt opened its next level. Keep it open and continue.
            nxt = await ex.eval(js.JS_LIST_OPTIONS)
            nxt = [o for o in (nxt if isinstance(nxt, list) else []) if _key(o) not in baseline_keys]
            if nxt and (not shown or PLACEHOLDER.match(shown) or LEVELISH.search(shown)):
                # a category opened its next level (Job Board -> Indeed / LinkedIn / ...): stay in it
                open_now = True
                options_seen_at_level = [o["text"] for o in nxt[:12]]
                last_err = f"inside {pick['text']!r} the list shows: {options_seen_at_level}; none matched the preference list"
                continue
            last_err = f"clicked {pick['text'][:60]!r} but the control then showed {shown[:60]!r}"
        if pick is None:
            sample = [o["text"] for o in options[:12]]
            last_err = f"no option matched {want!r}; saw: {sample}" if options else f"no options appeared after opening (typed {want!r})"
    # Last resort on a Workday prompt input: type the text and press Enter (free-entry prompts take the typed
    # text; filtered prompts take the highlighted match). Verified by a selected-item chip.
    if wd_prompt:
        whys = []
        for w in [x for x in wants if x and x.strip()][:3]:
            ok, shown = await _type_and_enter(ex, ref, [w, *[x for x in wants if x and x != w]], baseline_keys)
            if ok:
                return FieldResult(ref, True, shown)
            whys.append(f"{w[:30]!r}: {shown[:120]}")
        last_err += "; search-and-select: " + " | ".join(whys)
    await _close_open_lists(ex)
    return FieldResult(ref, False, before, last_err)


async def _close_open_lists(ex: Executor) -> None:
    """Escape, and when option rows are still showing, a click on the page's top-left corner (outside any
    menu), which is what closes a Workday 'Select One' list."""
    try:
        await ex.page.keyboard.press("Escape")
    except Exception:
        return
    await asyncio.sleep(0.2)
    try:
        still = await ex.eval(js.JS_LIST_OPTIONS)
    except Exception:
        return
    if not (isinstance(still, list) and still):
        return
    try:
        await ex.click_xy(2, 2)
        await asyncio.sleep(0.3)
    except Exception:
        pass


async def _committed(ex: Executor, ref: str, text: str) -> tuple[bool, str]:
    """(committed, why). Strict only for prompt widgets (Workday): a chip with the option text must exist.
    Other controls count as committed when the field shows no error."""
    st = await ex.eval(js.JS_FIELD_COMMIT, ref)
    if not isinstance(st, dict) or not st.get("found"):
        return True, ""            # the control was replaced by its committed rendering
    t = _norm(text)
    if st.get("cx") and st.get("cxInvalid"):
        # an Oracle picker that still says invalid holds typed text, not a pick (AutoZone's Gender read 'Male' and stayed
        # required, 2026-10-19); its validation can lag the click, so look once more
        await asyncio.sleep(0.6)
        st2 = await ex.eval(js.JS_FIELD_COMMIT, ref)
        if isinstance(st2, dict) and st2.get("found") and st2.get("cxInvalid"):
            return False, "the Oracle picker still marks the field invalid (the option was not taken)"
    if st.get("prompt"):
        if any(t and (t in _norm(c) or _norm(c) in t) for c in st.get("chips") or []):
            return True, ""
        return False, "no selected-item chip" + (f"; the field says {st['error']!r}" if st.get("error") else "")
    if st.get("invalid") and st.get("error") and not any(t in _norm(c) for c in st.get("chips") or []):
        # SAP SuccessFactors' paginated selects clear their 'required' error only once the box loses focus (L3Harris,
        # 2026-10-21: 'Not Protected Veteran (OFCCP)' was picked and reported not taken): leave the box, look again.
        try:
            await ex.eval("() => { const a = document.activeElement; if (a && a !== document.body && a.blur) a.blur(); return true; }")
            await asyncio.sleep(0.6)
            st3 = await ex.eval(js.JS_FIELD_COMMIT, ref)
            if isinstance(st3, dict) and (not st3.get("found") or not (st3.get("invalid") and st3.get("error"))):
                return True, ""
        except Exception:
            pass
        return False, f"the field still says {st['error']!r}"
    return True, ""


async def _force_commit(ex: Executor, ref: str, text: str, want: str) -> tuple[bool, str]:
    """Two more ways to take an option a plain click did not commit: tick the control inside the option
    (Workday menu items carry a radio), then the keyboard, with Enter pressed only while this field's list
    is open so it can never submit the form."""
    why = ""
    for step in ("inner-control", "keyboard"):
        try:
            await ex.page.keyboard.press("Escape")
            await ex.click_ref(ref)
            await asyncio.sleep(0.6)
            opened = await ex.eval(js.JS_LIST_OPTIONS)
            if not (isinstance(opened, list) and any(o.get("text") == text for o in opened)) and want and await _select_all_text(ex):
                await ex.type_text(want[:40], real_keys=True, delay_ms=20)
                await asyncio.sleep(1.2)
                opened = await ex.eval(js.JS_LIST_OPTIONS)
            listed = [o for o in (opened if isinstance(opened, list) else []) if o.get("text")]
            if not any(o["text"] == text for o in listed):
                why = "the option was not in the list when reopened"
                continue
            if step == "inner-control":
                await ex.eval(js.JS_COMMIT_OPTION, text)
            else:
                # Highlight the wanted option with the arrow keys, then Enter inside the open listbox.
                idx = next(i for i, o in enumerate(listed) if o["text"] == text)
                for _ in range(idx + 1):
                    await ex.page.keyboard.press("ArrowDown")
                    await asyncio.sleep(0.05)
                still_open = await ex.eval(js.JS_LIST_OPTIONS)
                if not (isinstance(still_open, list) and still_open):
                    why = "the list closed before Enter"
                    continue
                await ex.page.keyboard.press("Enter")
            await asyncio.sleep(0.7)
            ok, why = await _committed(ex, ref, text)
            if ok:
                return True, ""
        except Exception as e:
            why = f"{type(e).__name__}: {str(e)[:120]}"
    return False, why or "it did not commit"


async def _shows_file(ex: Executor, path: Path) -> bool:
    """True when the page already displays this file. Portals render the chip in their own wording
    (upper-cased, extension dropped), so match on the stem."""
    text = (await ex.page_text(30000)).lower()
    if path.stem.lower() in text or path.name.lower() in text:
        return True
    # SmartRecruiters draws the file chip inside a drop zone's shadow root ('Delete file <name>'): page text misses it
    try:
        return bool(await ex.eval(js.JS_FILE_SHOWN, [path.stem, path.name]))
    except Exception:
        return False


async def upload_file(ex: Executor, ref: str, path: Path) -> FieldResult:
    r = await _upload_file(ex, ref, path)
    if autosaves(ex):
        await ex.wait_saves(min_s=1.0, timeout_s=15.0)
    return r


async def _upload_file(ex: Executor, ref: str, path: Path) -> FieldResult:
    if not path.exists():
        return FieldResult(ref, False, "", f"file missing: {path.name}")
    # Never attach twice: after one upload the portal replaces the drop zone, and a second call lands
    # in the next empty slot (the cover-letter input) or wipes the first.
    if await _shows_file(ex, path):
        return FieldResult(ref, True, path.name)
    info = await ex.set_file(ref, str(path))
    shown = info.get("value") or ""
    ok = shown.lower() == path.name.lower()
    if not ok:
        # Some portals render the name elsewhere, upper-cased or without the extension, and only after
        # they have processed the upload (Greenhouse job boards show a spinner first): poll up to ~6 s.
        for _ in range(12):
            if await _shows_file(ex, path):
                ok, shown = True, path.name
                break
            await asyncio.sleep(0.5)
    return FieldResult(ref, ok, shown, "" if ok else f"page does not show {path.name}")


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"])}


def parse_date(value: str) -> dict:
    """'05/15/2028', '2028-05-15', 'May 2028', '05/2028' -> {'month': '05', 'day': '15', 'year': '2028'}."""
    v = str(value).strip()
    out: dict[str, str] = {}
    m = re.search(r"\b(19|20)\d{2}\b", v)
    if m:
        out["year"] = m.group(0)
    iso = re.match(r"^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?$", v)
    us = re.match(r"^(\d{1,2})[/-](?:(\d{1,2})[/-])?(\d{4})$", v)
    if iso:
        out["year"], out["month"] = iso.group(1), f"{int(iso.group(2)):02d}"
        if iso.group(3):
            out["day"] = f"{int(iso.group(3)):02d}"
    elif us:
        out["month"] = f"{int(us.group(1)):02d}"
        if us.group(2):
            out["day"] = f"{int(us.group(2)):02d}"
        out["year"] = us.group(3)
    else:
        for name, num in _MONTHS.items():
            if re.search(rf"\b{name[:3]}\w*\b", v, re.I):
                out["month"] = f"{num:02d}"
                break
        d = re.search(r"\b([0-3]?\d)(?:st|nd|rd|th)?\b(?!\d)", re.sub(r"\b(19|20)\d{2}\b", "", v))
        if d and "day" not in out and int(d.group(1)) <= 31:
            out["day"] = f"{int(d.group(1)):02d}"
    return out


async def set_date(ex: Executor, ref: str, value: str) -> FieldResult:
    """Fill a segmented date widget (Workday month/day/year spinbuttons) with real keystrokes, which is
    the only input they commit, then verify the widget no longer reads 'Invalid Date'."""
    parts = parse_date(value)
    if not parts:
        return FieldResult(ref, False, "", f"could not read a date out of {value!r}")
    # A native <input type="month"> / type="date" takes only its ISO value ('2026-09' / '2026-09-01'): Electronic Arts'
    # work-history start date stayed empty when '09/01/2026' was typed into it (2026-10-20).
    try:
        kind = await ex.eval("(r) => { const t = document.querySelector('[data-goat-ref=\"' + r + '\"]'); "
                             "return t && t.tagName === 'INPUT' ? (t.type || '') : ''; }", ref)
    except Exception:
        kind = ""
    if kind in ("month", "date") and parts.get("year") and parts.get("month"):
        iso = f'{parts["year"]}-{int(parts["month"]):02d}' + (f'-{int(parts.get("day") or 1):02d}' if kind == "date" else "")
        try:
            await ex.page.locator(f'[data-goat-ref="{ref}"]').first.fill(iso, timeout=3000)
        except Exception as e:  # noqa: BLE001 - reported below
            return FieldResult(ref, False, "", f"the {kind} input refused {iso}: {type(e).__name__}")
        now = (await ex.read_ref(ref)).get("value", "")
        return FieldResult(ref, now == iso, now, "" if now == iso else f"the {kind} input reads {now!r}, not {iso!r}")
    info = await ex.eval(js.JS_DATE_SECTIONS, ref)
    if not isinstance(info, dict) or not info.get("found"):
        # plain text date input: type it whole
        r = await fill_batch(ex, [{"ref": ref, "value": value}])
        return r[0] if r else FieldResult(ref, False, "", "no date sections and no input")
    for sec in info["sections"]:
        digits = parts.get(sec["part"])
        if not digits:
            continue
        # Workday's section inputs are zero-size spinbuttons under a visible "MM"/"YYYY" box: a click at their
        # centre often misses. Focus the input itself; fall back to a click.
        focused = False
        try:
            await ex.page.focus(f'[data-goat-ref="{sec["ref"]}"]', timeout=2000)
            focused = await ex.page.evaluate(
                "(r) => !!document.activeElement && document.activeElement.getAttribute('data-goat-ref') === r", sec["ref"])
        except Exception:
            focused = False
        if not focused:
            try:
                await ex.click_xy(int(sec["x"]), int(sec["y"]))
            except ClickRefused as e:
                return FieldResult(ref, False, "", str(e))
        await _select_all_text(ex)
        await ex.type_text(digits, real_keys=True, delay_ms=40)
        await asyncio.sleep(0.25)
    await _blur(ex)
    ok, shown, got = await _date_state(ex, ref, parts)
    if not ok:
        # Workday shows typed digits but can leave the date "Invalid Date" (GE Vernova and Cigna work-experience
        # From dates): its spinbuttons commit what the arrow keys set. Step each section to its value, then leave.
        after = await ex.eval(js.JS_DATE_SECTIONS, ref)
        for sec in (after or {}).get("sections", []) if isinstance(after, dict) else []:
            target = parts.get(sec["part"])
            if target:
                await _step_to(ex, ref, sec["part"], sec["ref"], int(target))
        await _blur(ex)
        ok, shown, got = await _date_state(ex, ref, parts)
    return FieldResult(ref, ok, shown or " ".join(f"{k}={v}" for k, v in got.items()),
                       "" if ok else f"date did not commit (widget reads {shown!r})")


async def _blur(ex: Executor) -> None:
    """Leave the date widget (Tab), which is when many of them commit."""
    try:
        await ex.page.keyboard.press("Tab")
    except Exception:
        pass
    await asyncio.sleep(0.4)


async def _date_state(ex: Executor, ref: str, parts: dict) -> tuple[bool, str, dict]:
    after = await ex.eval(js.JS_DATE_SECTIONS, ref)
    shown = (after or {}).get("text", "") if isinstance(after, dict) else ""
    got = {s["part"]: s.get("value", "") for s in (after or {}).get("sections", [])} if isinstance(after, dict) else {}

    def same(a, b) -> bool:
        try:
            return int(str(a or "0")) == int(str(b))
        except ValueError:
            return False

    ok = bool(got) and all(same(got.get(p), v) for p, v in parts.items() if p in got) and not (
        isinstance(after, dict) and after.get("invalid"))
    return ok, shown, got


async def _step_to(ex: Executor, ref: str, part: str, sec_ref: str, target: int) -> None:
    """ArrowUp / ArrowDown on one spinbutton until it reads the target (bounded: 12 months, 31 days, 60 years)."""
    limit = {"month": 13, "day": 32}.get(part, 61)
    try:
        await ex.page.focus(f'[data-goat-ref="{sec_ref}"]', timeout=2000)
    except Exception:
        return
    moved = False
    for _ in range(limit):
        st = await ex.eval(js.JS_DATE_SECTIONS, ref)
        cur = next((s.get("value", "") for s in (st or {}).get("sections", []) if s.get("part") == part), "") if isinstance(st, dict) else ""
        try:
            n = int(str(cur).strip() or "-1")
        except ValueError:
            n = -1
        if n == target:
            if not moved:
                # Shows the right digits but never committed them: a net-zero arrow round trip commits.
                await ex.page.keyboard.press("ArrowUp")
                await asyncio.sleep(0.05)
                await ex.page.keyboard.press("ArrowDown")
                await asyncio.sleep(0.05)
            return
        await ex.page.keyboard.press("ArrowUp" if n < target else "ArrowDown")
        moved = True
        await asyncio.sleep(0.05)


# Workday's dropdown button keeps an internal id in its value attribute ('993ab54bfe9f10010e330889cceb0001'). Read while
# the button re-rendered with no text, that id was taken for the pick, and Highmark Health's correct 'No, Opt - Out' was
# reported as a failure; the run then tried another answer key and handed the job to Surya (2026-10-20).
INTERNAL_ID = re.compile(r"^[0-9a-f]{24,40}$", re.I)


async def text_not_internal_id(ex: Executor, ref: str, shown: str) -> str:
    """The control's text, read again (up to three times) when what was read is an internal id; '' if it stays one."""
    if not INTERNAL_ID.match((shown or "").strip()):
        return shown
    for _ in range(3):
        if (shown or "").strip() and not INTERNAL_ID.match(shown.strip()):
            return shown
        await asyncio.sleep(0.6)
        try:
            again = await ex.read_ref(ref)
        except Exception:
            return ""
        shown = str(again.get("value") or again.get("shown") or "")
    return "" if INTERNAL_ID.match((shown or "").strip()) else shown


async def clear_cookie_wall(ex: Executor) -> dict | None:
    """Close a cookie / privacy layer that lies over the page: its decline or necessary-only button when it has one;
    its accept button only when the layer blocks the page and offers no decline (the codebase's standing rule for
    Workday banners, "Decline or Accept is fine", applied to every portal). Returns what was done, or None."""
    try:
        out = await ex.eval(js.JS_COOKIE_WALL)
    except Exception:
        return None
    if isinstance(out, dict) and out.get("clicked"):
        await asyncio.sleep(1.0)
    return out if isinstance(out, dict) else None


async def type_code_into(ex: Executor, refs: list[str], code: str) -> bool:
    """Segmented one-time-code boxes advance on each keystroke; a single box takes the whole code.
    The code is typed with keyboard events and never logged."""
    if not refs:
        return False
    await ex.focus_ref(refs[0])
    if len(refs) >= len(code):
        for i, ch in enumerate(code):
            await ex.page.keyboard.press(ch)
            await asyncio.sleep(0.05)
    else:
        await ex.type_secret(code)
    await asyncio.sleep(0.4)

    async def landed() -> bool:
        vals = []
        for r in refs:
            info = await ex.read_ref(r)
            vals.append(info.get("value", ""))
        return "".join(vals).replace(" ", "") == code or (len(refs) == 1 and vals[0] == code)
    if await landed():
        return True
    # The keystrokes went elsewhere (a layer over the boxes held the focus): set the boxes through the page itself,
    # one character each, which needs neither the focus nor a free click point (Texas Instruments, 2026-10-20).
    boxes = list(zip(refs, code)) if len(refs) >= len(code) > 1 else [(refs[0], code)]
    for r, part in boxes:
        try:
            await ex.page.locator(f'[data-goat-ref="{r}"]').first.fill(part, timeout=3000)
        except Exception:
            pass
    await asyncio.sleep(0.4)
    return await landed()


async def peek_options(ex: Executor, ref: str) -> tuple[list[str], bytes | None]:
    """Every choice a field offers, and a screenshot of it, without choosing anything. Native selects and
    radio groups are read directly; a custom dropdown is opened, read (and photographed open, so the model
    sees the whole list) and closed again with Escape."""
    info = await ex.read_ref(ref)            # also scrolls the field into the middle of the view
    if not info.get("found"):
        return [], None

    async def crop() -> bytes | None:
        try:
            y = int(info.get("y", 0))
            top = max(0, y - 260)
            return await ex.zoom_png(0, top, ex.vw, min(ex.vh - top, 620))
        except Exception:
            try:
                return await ex.screenshot_png()
            except Exception:
                return None

    native = await ex.eval(js.JS_FIELD_CHOICES, ref)
    if isinstance(native, list) and native:
        return [str(x) for x in native][:60], await crop()
    # react-select exposes its options without opening
    listed = await ex.eval(js.JS_REACT_SELECT, [ref, None])
    if isinstance(listed, dict) and listed.get("list") and listed.get("options"):
        return [str(x) for x in listed["options"]][:60], await crop()
    # A plain text box has no list to open (a combobox input carries aria-expanded, so it is opened below).
    plain_text = info.get("type") in ("text", "email", "tel", "number", "date", "") and info.get("expanded") is None
    if info.get("tag") == "textarea" or (info.get("tag") == "input" and plain_text):
        return [], await crop()
    baseline = await ex.eval(js.JS_LIST_OPTIONS)
    seen = {(o.get("text"), o.get("x"), o.get("y")) for o in (baseline if isinstance(baseline, list) else [])}
    texts: list[str] = []
    png = None
    try:
        await ex.page.keyboard.press("Escape")
        await ex.click_ref(ref)
        await asyncio.sleep(0.7)
        opened = await ex.eval(js.JS_LIST_OPTIONS)
        texts = [o["text"] for o in (opened if isinstance(opened, list) else []) if (o.get("text"), o.get("x"), o.get("y")) not in seen]
        png = await crop()
    except Exception:
        pass
    finally:
        try:
            await ex.page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
        except Exception:
            pass
    return list(dict.fromkeys(texts))[:60], png or await crop()
