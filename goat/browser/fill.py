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

PLACEHOLDER = re.compile(r"^(select one|select\.\.\.|select…|select|choose one|choose|search|please select|--|)$", re.I)


@dataclass
class FieldResult:
    ref: str
    ok: bool
    readback: str
    error: str = ""

    def as_dict(self) -> dict:
        return {"ref": self.ref, "status": "set" if self.ok else "failed", "readback": self.readback, "error": self.error}


async def fill_batch(ex: Executor, fields: list[dict]) -> list[FieldResult]:
    """fields: [{ref, value}] where value is str or bool. Password and file inputs are refused."""
    if not fields:
        return []
    raw = await ex.eval(js.JS_FILL_BATCH, fields)
    out = []
    for r in raw if isinstance(raw, list) else []:
        out.append(FieldResult(ref=r.get("ref", ""), ok=bool(r.get("ok")), readback=str(r.get("readback", "")),
                               error=str(r.get("error", ""))))
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


AFFIRM_OPT = re.compile(r"^(yes\b|i (agree|consent|accept|authori[sz]e|acknowledge|understand|certify|confirm)\b|agree\b|accept\b)", re.I)
NEG_OPT = re.compile(r"^(no\b|i do not\b|i don'?t\b|please do not\b|do not\b|don'?t\b|i decline\b|decline\b|not\b|never\b)", re.I)


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


def best_option(options: list[dict], wants: list[str]) -> dict | None:
    """Exact match first. A prefix/contains match is accepted only when it is UNambiguous: if several
    options share the prefix (Texas A&M University - College Station / - Commerce / - Kingsville) the
    caller must name the exact one. Picking the shortest would silently choose the wrong campus."""
    seen: set[str] = set()
    options = [o for o in options if not (_norm(o.get("text")) in seen or seen.add(_norm(o.get("text"))))]
    for want in wants:
        w = _norm(want)
        if not w:
            continue
        exact = [o for o in options if _norm(o["text"]) == w]
        if exact:
            return exact[0]
        starts = [o for o in options if _norm(o["text"]).startswith(w)]
        if len(starts) == 1:
            return starts[0]
        if len(starts) > 1:
            # Allowed only when the extra text is a parenthetical/qualifier of the same thing, e.g. "Yes (I am)".
            # (on the raw text: _norm strips the parentheses this rule looks for)
            bare = [o for o in starts if re.fullmatch(re.escape(want.strip().lower()) + r"\s*(\(.*\))?", (o["text"] or "").strip().lower())]
            if len(bare) == 1:
                return bare[0]
            continue
        has = [o for o in options if w in _norm(o["text"])]
        if len(has) == 1:
            return has[0]
        # "Bachelor's" vs "Bachelors", "E-mail" vs "Email": equal once spaces and punctuation are gone.
        sq = [o for o in options if _squash(o["text"]) == _squash(want)]
        if len(sq) == 1:
            return sq[0]
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


async def choose(ex: Executor, ref: str, wants: list[str], *, type_to_filter: bool = True) -> FieldResult:
    """Pick an option in a custom dropdown / combobox / autocomplete.

    Order: (1) react-select props when the control is one; (2) click to open, optionally type the
    first letters with real keys, read the visible options, click the best match; (3) keyboard
    fallback (type + Enter is NOT used blindly: Enter can submit a form). Verified by read-back."""
    info = await ex.read_ref(ref)
    if not info.get("found"):
        return FieldResult(ref, False, "", "ref not found")
    before = (info.get("value") or info.get("shown") or "")
    # Workday multi-select trees (How Did You Hear About Us?) count their picks ("0 items selected") and nest
    # options under categories: the prompt's search box filters the whole tree, so each want is typed and
    # matched exactly instead of drilling into a category by hand.
    multi_tree = bool(re.search(r"\bitems? selected\b", before or "", re.I))
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

    baseline = await ex.eval(js.JS_LIST_OPTIONS)
    baseline_keys = {_key(o) for o in (baseline if isinstance(baseline, list) else [])}
    open_now = False
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
            if type_to_filter and want and not (shown_now and best_option(shown_now, [want] if multi_tree else wants)) \
                    and await _text_box_focused(ex):
                try:
                    await ex.page.keyboard.press("Control+a")     # replace, never append to, text left in the search box
                except Exception:
                    pass
                await ex.type_text(want[:40], real_keys=True, delay_ms=20)
                await asyncio.sleep(1.2)
        options = await ex.eval(js.JS_LIST_OPTIONS)
        options = options if isinstance(options, list) else []
        new_opts = [o for o in options if _key(o) not in baseline_keys]
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
                try:
                    await ex.page.keyboard.press("Control+a")
                except Exception:
                    pass
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
            after = await ex.read_ref(ref)
            shown = (after.get("value") or after.get("shown") or "")
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
                return FieldResult(ref, True, after.get("shown", pick["text"]))
            # still a placeholder: a multi-level prompt opened its next level. Keep it open and continue.
            nxt = await ex.eval(js.JS_LIST_OPTIONS)
            nxt = [o for o in (nxt if isinstance(nxt, list) else []) if _key(o) not in baseline_keys]
            if nxt and (not shown or PLACEHOLDER.match(shown) or LEVELISH.search(shown)):
                # a category opened its next level (Job Board -> Indeed / LinkedIn / ...): stay in it
                open_now = True
                options_seen_at_level = [o["text"] for o in nxt[:12]]
                last_err = f"inside {pick['text']!r} the list shows: {options_seen_at_level}; none matched the preference list"
                continue
        if pick is None:
            sample = [o["text"] for o in options[:12]]
            last_err = f"no option matched {want!r}; saw: {sample}" if options else f"no options appeared after opening (typed {want!r})"
    try:
        await ex.page.keyboard.press("Escape")
    except Exception:
        pass
    return FieldResult(ref, False, before, last_err)


async def _committed(ex: Executor, ref: str, text: str) -> tuple[bool, str]:
    """(committed, why). Strict only for prompt widgets (Workday): a chip with the option text must exist.
    Other controls count as committed when the field shows no error."""
    st = await ex.eval(js.JS_FIELD_COMMIT, ref)
    if not isinstance(st, dict) or not st.get("found"):
        return True, ""            # the control was replaced by its committed rendering
    t = _norm(text)
    if st.get("prompt"):
        if any(t and (t in _norm(c) or _norm(c) in t) for c in st.get("chips") or []):
            return True, ""
        return False, "no selected-item chip" + (f"; the field says {st['error']!r}" if st.get("error") else "")
    if st.get("invalid") and st.get("error") and not any(t in _norm(c) for c in st.get("chips") or []):
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
            if not (isinstance(opened, list) and any(o.get("text") == text for o in opened)) and want:
                await ex.page.keyboard.press("Control+a")
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
    return path.stem.lower() in text or path.name.lower() in text


async def upload_file(ex: Executor, ref: str, path: Path) -> FieldResult:
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
        await ex.page.keyboard.press("Control+a")
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
    vals = []
    for r in refs:
        info = await ex.read_ref(r)
        vals.append(info.get("value", ""))
    joined = "".join(vals)
    return joined.replace(" ", "") == code or (len(refs) == 1 and vals[0] == code)


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
