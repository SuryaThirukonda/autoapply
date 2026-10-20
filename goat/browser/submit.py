"""The only path to submission. Scans the page and refuses to click while anything is wrong, commits a
`submitting` marker, clicks once, then demands a positive signal. Returns submitted | blocked | failed;
there is no "unclear"."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from .. import db, states
from . import js
from .errors import scan_errors, submit_signal, why_blocked
from .executor import Executor, ClickRefused


@dataclass
class SubmitResult:
    status: str                      # submitted | blocked | failed
    signal: str = ""
    why: list[str] = field(default_factory=list)
    url: str = ""
    clicked_label: str = ""

    def as_dict(self) -> dict:
        return {"status": self.status, "signal": self.signal, "why": self.why, "url": self.url,
                "clicked_label": self.clicked_label}


async def submit(ex: Executor, job_id: int | None, *, label: str = "", wait_s: float = 15.0) -> SubmitResult:
    scan = await scan_errors(ex)
    why = why_blocked(scan)
    if why:
        return SubmitResult("blocked", why=why, url=scan.get("url", ex.url))

    from .captcha import detect_challenge
    ch = await detect_challenge(ex)
    if ch.get("challenge"):
        return SubmitResult("blocked", why=[f"human-verification challenge present: {', '.join(ch.get('kinds') or [])}"], url=ex.url)

    found = await ex.eval(js.JS_FIND_SUBMIT, label)
    if not isinstance(found, dict) or not found.get("ok"):
        tried = found.get("tried", 0) if isinstance(found, dict) else 0
        return SubmitResult("blocked", why=[f"no enabled submit control found (label={label or 'any submit-ish'}; {tried} candidates)"], url=ex.url)

    # The click point must be the page's own submit control, not an extension panel.
    el = await ex.element_at(int(found["x"]), int(found["y"]))
    if el.captcha:
        return SubmitResult("blocked", why=["submit control is covered by a human-verification challenge"], url=ex.url)
    if not el.submit:
        return SubmitResult("blocked", why=[f"element at the submit position is not a submit control ({el.tag}: {el.text[:60]})"], url=ex.url)

    before = await submit_signal(ex)
    if job_id is not None:
        with db.tx() as con:
            con.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (states.SUBMITTING, db.now(), job_id))
        db.event(job_id, "submit:clicking", {"label": found.get("text"), "url": before.get("url")})

    try:
        await ex.click_xy(int(found["x"]), int(found["y"]), allow_submit=True)
    except ClickRefused as e:
        return SubmitResult("blocked", why=[str(e)], url=ex.url)

    deadline = time.monotonic() + max(2.0, wait_s)
    last = before
    while time.monotonic() < deadline:
        await asyncio.sleep(1.0)
        try:
            last = await submit_signal(ex)
        except Exception:
            await asyncio.sleep(1.0)  # navigation tearing down the context is itself movement
            continue
        if last.get("confirm_match"):
            return SubmitResult("submitted", signal="confirmation_text: " + last["confirm_match"], url=last.get("url", ""),
                                clicked_label=found.get("text", ""))
        if last.get("url") and before.get("url") and last["url"] != before["url"]:
            return SubmitResult("submitted", signal=f"url_changed: {before['url']} -> {last['url']}", url=last.get("url", ""),
                                clicked_label=found.get("text", ""))
        if before.get("submit_buttons", 0) > 0 and last.get("submit_buttons", 0) == 0 and last.get("forms", 1) == 0:
            return SubmitResult("submitted", signal="form_and_submit_button_gone", url=last.get("url", ""),
                                clicked_label=found.get("text", ""))
        # A page that shows a new error after the click is a definite failure: stop waiting.
        try:
            after_scan = await scan_errors(ex)
        except Exception:
            # the page navigated away under the scan (Hy-Vee's submit went through and the scan crashed the tool,
            # 2026-10-20): that is movement; the next signal check reads the new page
            await asyncio.sleep(1.0)
            continue
        refused = why_blocked(after_scan) + [f"form summary: {x.get('text')}" for x in (after_scan.get("summaries") or [])]
        if refused:
            return SubmitResult("failed", why=refused, url=after_scan.get("url", ""), clicked_label=found.get("text", ""))

    try:
        after = await scan_errors(ex)
    except Exception:
        after = {}
        try:
            last = await submit_signal(ex)
        except Exception:
            last = {}
        if last.get("url") and before.get("url") and last["url"] != before["url"]:
            return SubmitResult("submitted", signal=f"url_changed: {before['url']} -> {last['url']}", url=last.get("url", ""),
                                clicked_label=found.get("text", ""))
    return SubmitResult("failed", why=why_blocked(after) or ["page did not change and showed no error; the control may be inert"],
                        url=after.get("url", ""), clicked_label=found.get("text", ""))
