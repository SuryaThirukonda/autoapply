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


# A chat widget over the submit button (LLNL's SmartRecruiters 'Chatbot' iframe from winstonchat, z-index 2147483647,
# 2026-10-21): it is no part of the application, so it is hidden and the point probed again.
JS_HIDE_CHAT_AT = r"""([x, y]) => {
  const el = document.elementFromPoint(x, y);
  if (!el) return '';
  const host = el.closest('iframe, [class*="widget" i], [id*="chat" i], [class*="chat" i]') || el;
  const desc = [host.tagName, host.id || '', String(host.className || ''), host.getAttribute('title') || '', host.getAttribute('src') || ''].join(' ');
  if (!/chat|\bbot\b|intercom|drift|zendesk|livechat|joveo|winston|messenger/i.test(desc)) return '';
  host.style.setProperty('display', 'none', 'important');
  return desc.slice(0, 160);
}"""


def same_form_move(a: str, b: str) -> bool:
    """A move between sections of one application form, not a submission: Oracle went from /apply/section/1 to
    /apply/section/3 to show 'You have 2 issues that need to be fixed before you can submit your application', and
    AutoZone was recorded as submitted (2026-10-21)."""
    import re
    cut = [re.split(r"[?#]", u or "", maxsplit=1)[0] for u in (a, b)]
    m = [re.match(r"^(.*?/apply)/section/\d+/?$", u) for u in cut]
    return bool(m[0] and m[1] and m[0].group(1) == m[1].group(1))


async def _issue_banner(ex: Executor) -> str:
    """Oracle's own count of what blocks its submit, when the page shows it."""
    from ..gate import ORACLE_ISSUES
    try:
        m = ORACLE_ISSUES.search(await ex.page_text(30000) or "")
    except Exception:
        return ""
    return m.group(0) if m else ""


LATE_CONFIRM_S = 30.0     # after wait_s with no change: keep watching for a slow portal's confirmation


async def submit(ex: Executor, job_id: int | None, *, label: str = "", wait_s: float = 15.0) -> SubmitResult:
    scan = await scan_errors(ex)
    # The gate's stale-message rule applies here too: SuccessFactors keeps 'Please complete your personal information.'
    # on screen after every field is set; CenturyLink passed the gate, the judge and the check, and submit refused it on
    # that message alone (2026-10-21).
    from ..gate import drop_stale_required
    scan = drop_stale_required(scan, None)
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
    if not el.submit and not el.captcha:
        try:
            hidden = await ex.eval(JS_HIDE_CHAT_AT, [int(found["x"]), int(found["y"])])
        except Exception:
            hidden = ""
        if hidden:
            if job_id is not None:
                db.event(job_id, "submit:chat_widget_hidden", {"what": str(hidden)[:160]})
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
        issue = await _issue_banner(ex)
        if issue:
            return SubmitResult("failed", why=[f"the portal says {issue!r}: the application is not submitted"],
                                url=last.get("url", "") or ex.url, clicked_label=found.get("text", ""))
        if last.get("url") and before.get("url") and last["url"] != before["url"] and not same_form_move(before["url"], last["url"]):
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
        # Folded sections are page layout, not an error from the click: SuccessFactors' 'Successfully Applied' page keeps its
        # profile sections folded, and NetApp's submission was called failed one second after the click (2026-10-21).
        refused = [r for r in why_blocked(after_scan) if not str(r).startswith("collapsed_sections")] \
            + [f"form summary: {x.get('text')}" for x in (after_scan.get("summaries") or [])]
        if refused:
            return SubmitResult("failed", why=refused, url=after_scan.get("url", ""), clicked_label=found.get("text", ""))

    # Nothing moved yet: a slow portal confirms well after the wait. L3Harris' SuccessFactors showed 'Successfully Applied'
    # about 20 s after the Apply click; the submit was called failed at 15 s and the run held 'stuck' over an application
    # that had gone in (2026-10-23). Watch a while longer, for a confirmation, a new address or an issue banner only.
    late_deadline = time.monotonic() + LATE_CONFIRM_S
    while time.monotonic() < late_deadline:
        await asyncio.sleep(2.0)
        try:
            last = await submit_signal(ex)
        except Exception:
            continue
        if last.get("confirm_match"):
            return SubmitResult("submitted", signal="confirmation_text (late): " + last["confirm_match"], url=last.get("url", ""),
                                clicked_label=found.get("text", ""))
        if last.get("url") and before.get("url") and last["url"] != before["url"] and not same_form_move(before["url"], last["url"]):
            return SubmitResult("submitted", signal=f"url_changed (late): {before['url']} -> {last['url']}", url=last.get("url", ""),
                                clicked_label=found.get("text", ""))
        issue = await _issue_banner(ex)
        if issue:
            return SubmitResult("failed", why=[f"the portal says {issue!r}: the application is not submitted"],
                                url=last.get("url", "") or ex.url, clicked_label=found.get("text", ""))
    try:
        after = await scan_errors(ex)
    except Exception:
        after = {}
        try:
            last = await submit_signal(ex)
        except Exception:
            last = {}
        if last.get("url") and before.get("url") and last["url"] != before["url"] and not same_form_move(before["url"], last["url"]):
            return SubmitResult("submitted", signal=f"url_changed: {before['url']} -> {last['url']}", url=last.get("url", ""),
                                clicked_label=found.get("text", ""))
    return SubmitResult("failed", why=[r for r in why_blocked(after) if not str(r).startswith("collapsed_sections")]
                        or ["page did not change and showed no error; the control may be inert"],
                        url=after.get("url", ""), clicked_label=found.get("text", ""))
