"""Pruned accessibility-style outline of the page with stable refs. The error summary comes first
so a line like "No location found" is text the model cannot miss. Password values never appear."""
from __future__ import annotations

import asyncio

from dataclasses import dataclass, field

from . import js
from .errors import scan_errors, format_errors
from .captcha import detect_challenge
from .executor import Executor

MAX_ITEMS = 220
MAX_CHARS = 14000


@dataclass
class Snapshot:
    url: str
    title: str
    headings: list[str]
    items: list[dict]
    groups: list[dict]
    errors: dict
    challenge: dict
    text: str = ""
    scroll_y: int = 0
    page_height: int = 0
    total_items: int = 0
    refs: dict[str, dict] = field(default_factory=dict)
    unsaved: list[dict] = field(default_factory=list)   # answers an autosaving form shows but has not stored (Ashby)
    tiles: list[dict] = field(default_factory=list)     # Oracle's entry tiles: section, title, sub, invalid, edit, del
    entry_form_open: bool = False                       # an Oracle Experience / Education entry form is open (unsaved)
    required_blocks: list[str] = field(default_factory=list)   # Oracle blocks marked Required ('Work History')

    def find(self, ref: str) -> dict | None:
        return self.refs.get(ref)

    def required_empty(self) -> list[dict]:
        return list(self.errors.get("empty_required", []))

    def file_inputs(self) -> list[dict]:
        return [i for i in self.items if i.get("type") == "file"]

    def submit_buttons(self) -> list[dict]:
        return [i for i in self.items if i.get("submitish")]


def _fmt_item(i: dict) -> str:
    tag = i.get("tag")
    typ = i.get("type") or ""
    role = i.get("role") or ""
    label = (i.get("label") or "").replace('"', "'")
    pos = f"@({i['bbox'][0]},{i['bbox'][1]})" if i.get("bbox") else ""
    flags = []
    if i.get("required"):
        flags.append("required")
    if i.get("invalid"):
        flags.append("INVALID")
    if i.get("disabled"):
        flags.append("disabled")
    if not i.get("inview", True):
        flags.append("offscreen")
    if i.get("max_chars"):
        flags.append(f"max {i['max_chars']} chars")
    fl = (" " + " ".join(flags)) if flags else ""
    if tag == "input" and typ == "checkbox":
        return f'[{i["ref"]}] checkbox "{label}"{fl} [{"x" if i.get("checked") else " "}] {pos}'
    if tag in ("input", "textarea", "richtext"):
        kind = "textbox" if tag != "input" else (typ or "text")
        if typ == "file":
            kind = "file"
            nm = f' name="{i["name"]}"' if i.get("name") else ""
            extra = nm + ' value="%s"' % i.get("value", "") + (" (hidden input; use upload_resume)" if i.get("hidden") else "")
            return f'[{i["ref"]}] file "{label}"{fl}{extra} {pos}'
        val = i.get("shown") or i.get("value") or ""
        combo = " combobox" if i.get("combo") else ""
        ph = f' placeholder="{i["placeholder"][:40]}"' if i.get("placeholder") and not val else ""
        # The outline keeps the first 300 characters of a value; a longer answer is NOT cut in the box (IEX, IXL
        # 2026-10-19: the judge read "...when p" as a truncated essay that the box held in full).
        more = (f' (the box holds all {i["value_len"]} characters; the outline shows the first {len(val)})'
                if i.get("value_len") and int(i["value_len"]) > len(val) else "")
        return f'[{i["ref"]}] {kind}{combo} "{label}"{fl} value="{val}"{more}{ph} {pos}'
    if tag == "select":
        opts = i.get("options") or []
        more = f" (+{i.get('noptions', 0) - len(opts)} more)" if i.get("noptions", 0) > len(opts) else ""
        return f'[{i["ref"]}] select "{label}"{fl} value="{i.get("value", "")}" options: {" | ".join(opts)}{more} {pos}'
    if tag == "dropdown":
        return f'[{i["ref"]}] dropdown "{label}"{fl} value="{i.get("value", "")}" (custom; use choose) {pos}'
    if tag == "toggle":
        g = (i.get("group") or "").replace('"', "'")[:120]
        return f'[{i["ref"]}] toggle "{label}" [{"x" if i.get("pressed") else " "}] for question "{g}" (set with fill_batch + answer_key) {pos}'
    if tag == "button":
        s = " SUBMIT-CONTROL(only submit() may click)" if i.get("submitish") else ""
        return f'[{i["ref"]}] button "{label}"{fl}{s} {pos}'
    if tag == "a":
        return f'[{i["ref"]}] link "{label}" {pos}'
    if tag == "option":
        return f'[{i["ref"]}] option "{label}"{" selected" if i.get("selected") else ""} {pos}'
    return f'[{i["ref"]}] {role or tag} "{label}"{fl} {pos}'


def _fmt_group(g: dict) -> str:
    opts = " ".join(f'({o["ref"]}) "{(o["label"] or "").replace(chr(34), "")[:60]}" [{"x" if o.get("checked") else " "}]' for o in g.get("options", []))
    fl = " required" if g.get("required") else ""
    fl += " INVALID" if g.get("invalid") else ""
    return f'{g.get("kind")} group "{(g.get("label") or "").replace(chr(34), "")[:200]}"{fl}: {opts}'


def render(s: Snapshot, max_chars: int = MAX_CHARS) -> str:
    lines = [f"URL: {s.url}", f"TITLE: {s.title}"]
    err = format_errors(s.errors)
    lines.append("ERRORS: " + (err if err else "none"))
    if s.challenge.get("challenge"):
        lines.append(f"HUMAN-VERIFICATION CHALLENGE PRESENT: {', '.join(s.challenge.get('kinds') or ['unknown'])} (never click it; finish the form then call park)")
    if s.challenge.get("email_code"):
        lines.append(f"EMAILED CODE PAGE: {s.challenge.get('code_inputs')} code boxes (call enter_email_code; the code never comes to you)")
    if s.headings:
        lines.append("HEADINGS: " + " | ".join(s.headings[:8]))
    for u in s.unsaved[:6]:
        lines.append(f"NOT STORED BY THE FORM: '{u.get('title', '')[:80]}' shows {u.get('shown', '')!r} but the portal saved "
                     f"{u.get('saved', '') or 'nothing'!r} (tick another option, then this one again)")
    if s.entry_form_open:
        lines.append("ENTRY FORM OPEN: an Experience / Education entry form is open; fill it and click its SAVE (or CANCEL it)")
    for t in s.tiles[:12]:
        where = " (on another step of the form)" if t.get("visible") is False else ""
        lines.append(f"ENTRY TILE ({t.get('section') or 'section'}){where}: '{t.get('title', '')}' / '{t.get('sub', '')}'"
                     f"{' FIELDS TO FIX' if t.get('invalid') else ''} [Edit {t.get('edit') or '?'} | Delete {t.get('del') or '?'}]")
    lines.append(f"VIEWPORT scrollY={s.scroll_y} pageHeight={s.page_height} ({s.total_items} interactive elements, {len(s.items)} listed)")
    for g in s.groups:
        lines.append(_fmt_group(g))
    for i in s.items:
        lines.append(_fmt_item(i))
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[: max_chars - 40] + "\n...[outline truncated; scroll or zoom for more]"
    return out


async def keep_session_alive(ex: Executor) -> bool:
    """Press "Continue" on a session-timeout prompt (Oracle's "Are You Still With Us?"). True when one was dismissed."""
    try:
        r = await ex.eval(js.JS_KEEPALIVE_DIALOG)
    except Exception:
        return False
    if isinstance(r, dict) and r.get("dismissed"):
        ex._log("keepalive", {"text": r.get("text", "")[:120], "button": r.get("button", "")})
        try:
            from .. import db
            db.event(ex.job_id, "page:keepalive", {"text": r.get("text", "")[:160], "button": r.get("button", "")})
        except Exception:
            pass
        await asyncio.sleep(0.6)
        return True
    return False


async def take(ex: Executor, max_items: int = MAX_ITEMS, with_text: bool = False) -> Snapshot:
    await keep_session_alive(ex)
    raw = await ex.eval(js.JS_SNAPSHOT, max_items)
    if not isinstance(raw, dict):
        raw = {"url": ex.url, "title": "", "headings": [], "items": [], "groups": [], "total": 0}
    errors = await scan_errors(ex)
    challenge = await detect_challenge(ex)
    snap = Snapshot(url=raw.get("url", ""), title=raw.get("title", ""), headings=raw.get("headings", []),
                    items=raw.get("items", []), groups=raw.get("groups", []), errors=errors, challenge=challenge,
                    scroll_y=raw.get("scrollY", 0), page_height=raw.get("pageHeight", 0), total_items=raw.get("total", 0),
                    text=raw.get("text_head", ""), tiles=raw.get("tiles") or [], entry_form_open=bool(raw.get("entry_form_open")),
                    required_blocks=list(raw.get("required_blocks") or []))
    for i in snap.items:
        snap.refs[i["ref"]] = i
    for g in snap.groups:
        for o in g.get("options", []):
            snap.refs[o["ref"]] = {**o, "tag": "input", "type": g.get("kind"), "group": g.get("label")}
    if "ashbyhq.com" in (snap.url or ex.url or "").lower():
        try:
            u = await ex.eval(js.JS_ASHBY_UNSAVED)
            snap.unsaved = u if isinstance(u, list) else []
        except Exception:
            snap.unsaved = []
    if with_text:
        snap.text = await ex.page_text(6000)
    return snap
