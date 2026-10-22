"""The single worker: claim -> lane -> open posting -> adapter prefill -> agent loop (one conversation
per form page) -> gate/submit inside the loop -> ledger. Every exit has a name."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from . import answers, db, ledger, records, states
from .adapters import for_portal
from .agent.classifier import Classifier
from .agent.loop import AgentLoop, LoopResult
from .agent.tools import RunContext
from .browser import chrome, snapshot as snapmod
from .browser.executor import Executor, HostPolicy
from .budget import Budget, BudgetExceeded, Parked
from .config import SETTINGS
from .human.questions import BROKER

log = logging.getLogger(__name__)


# Surya (2026-10-21): "ensure that the queue sorts by post date for the internship, where recent is at the top ... and
# the oldest is later in the queue. everything within 1-2 days posted should be applied immediately". His explicit
# choices stay first: a release (+5000), a pin (10 000), his drag order on the website and `run-once --job` (1000 and up),
# and a job he unblocked by answering its question. Every other queued job is re-ranked by posting date at each claim.
EXPLICIT_PRIORITY = 1000      # = cloud.ORDER_BASE
AUTO_TOP = 899                # auto ranks stay below 900 so a requeue's +5 / +10 never crosses into the explicit band
FRESH_DAYS = 2
ALIGNED_DAYS = 7              # a posting this recent whose title fits his roles (triage.role_fit) leads the queue


def rank_queue(con, now: int | None = None) -> int:
    """Give the queued jobs below EXPLICIT_PRIORITY priorities 899, 898, ... newest posting first (date_posted, else the
    day goat first saw the listing). Postings from the last ALIGNED_DAYS days whose title fits his roles (SWE, SDE,
    application development, data science / engineering / analytics: triage.role_fit) lead, then the rest of the last
    FRESH_DAYS days, then everything else (Surya, 2026-10-21). Every view sorted by priority (the website, `goat list`)
    then shows the claim order. Returns how many priorities changed."""
    from . import triage
    now = int(now or db.now())
    rows = con.execute(
        "SELECT j.id, j.priority, COALESCE(l.date_posted, l.first_seen, 0) AS posted, COALESCE(j.created_at, 0) AS created, "
        "COALESCE(l.title, '') AS title FROM jobs j JOIN listings l ON l.id=j.listing_id "
        "WHERE j.status=? AND COALESCE(j.priority, 0) < ? AND COALESCE(j.cleared_live, 0)=0",
        (states.QUEUED, EXPLICIT_PRIORITY)).fetchall()
    cutoff = now - FRESH_DAYS * 86400
    aligned_cutoff = now - ALIGNED_DAYS * 86400

    def fits(title: str) -> bool:
        try:
            return triage.role_fit(title) == triage.ALIGNED
        except Exception:  # noqa: BLE001 - without the rules file the posting order alone stands
            return False

    order = sorted(rows, key=lambda r: (int(r["posted"] or 0) >= aligned_cutoff and fits(r["title"]),
                                        int(r["posted"] or 0) >= cutoff, int(r["posted"] or 0), int(r["priority"] or 0),
                                        int(r["created"] or 0), int(r["id"])), reverse=True)
    changed = 0
    for rank, r in enumerate(order):
        p = max(1, AUTO_TOP - rank)
        if p != int(r["priority"] or 0):
            con.execute("UPDATE jobs SET priority=? WHERE id=?", (p, r["id"]))
            changed += 1
    return changed


# SAP SuccessFactors keeps one session per server for every company it hosts (career4: CenturyLink, Phillips 66,
# NetApp, Corning, Westinghouse). Right after CenturyLink's run, Phillips 66's 'Apply now' opened "An error occurred
# while processing your request", and NetApp's had the same (2026-10-21). A fresh SuccessFactors job starts signed out.
SF_COOKIE_DOMAIN = re.compile(r".*successfactors\.(com|eu)")


async def clear_sf_session(ex) -> bool:
    """Drop SuccessFactors' session cookies from the browser context; True when it was done."""
    try:
        await ex._context.clear_cookies(domain=SF_COOKIE_DOMAIN)
        return True
    except Exception:  # noqa: BLE001 - best effort
        return False


def claim_next() -> dict | None:
    """Surya's explicit choices first (releases, pins, his drag order, answered questions), then the newest postings
    (rank_queue). One claim at a time."""
    lanes = SETTINGS.lanes_enabled or ["no_account"]
    marks = ",".join("?" for _ in lanes)
    # portals the worker never claims (SKIP_ATS; iCIMS by default: an hCaptcha guards its account creation)
    skip = [a.lower() for a in (SETTINGS.skip_ats or [])] or ["-"]
    skip_marks = ",".join("?" for _ in skip)
    with db.tx() as con:
        rank_queue(con)
        row = con.execute(
            f"SELECT j.*, l.company, l.title, l.url, l.ats, l.variant, l.locations FROM jobs j JOIN listings l ON l.id=j.listing_id "
            f"WHERE j.status=? AND j.attempts < ? AND j.lane IN ({marks}) AND LOWER(COALESCE(l.ats, '')) NOT IN ({skip_marks}) "
            "ORDER BY j.priority DESC, l.date_posted DESC, j.created_at DESC LIMIT 1",
            (states.QUEUED, SETTINGS.budgets.max_attempts, *lanes, *skip)).fetchone()
        if not row:
            return None
        job = dict(row)
        con.execute("UPDATE jobs SET status=?, claimed_at=?, updated_at=?, attempts=attempts+1 WHERE id=?",
                    (states.FILLING, db.now(), db.now(), job["id"]))
        # Decisions belong to one attempt: archive the previous attempt's rows, then start clean. A draft Surya released
        # as filled keeps them while its tab may still hold the form he approved (run_job archives them if it does not).
        if not keeps_decisions(job) and not resumed_after_restart(con, job):
            archive_decisions(con, int(job["id"]))
        job["attempts"] = job["attempts"]  # attempts before this one
        db.event(job["id"], "job:claimed", {"company": job["company"], "title": job["title"], "ats": job["ats"], "attempt": job["attempts"] + 1})
        return job


def keeps_decisions(job: dict) -> bool:
    """A released draft whose tab is still known: its decisions are the ones Surya approved, so they stay."""
    return bool(job.get("cleared_live")) and bool(job.get("tab_target_id"))


def resumed_after_restart(con, job: dict) -> bool:
    """A job the agent was working on when it restarted (a crash or a reload) and whose tab is still known: the run
    re-attaches to that tab, which still holds what it filled, so the decisions behind those values stay. Archived,
    they left Aerospace Corporation's resumed My Experience page 'unsourced' and the gate refused it (2026-10-19)."""
    if not job.get("tab_target_id"):
        return False
    r = con.execute("SELECT kind, detail FROM events WHERE job_id=? AND kind IN ('job:requeued', 'job:claimed', 'job:hold', "
                    "'job:ready_for_you', 'job:dead', 'job:submitted', 'job:released') ORDER BY n DESC LIMIT 1", (job["id"],)).fetchone()
    return bool(r and r["kind"] == "job:requeued" and "restart while in flight" in (r["detail"] or ""))


def archive_decisions(con, job_id: int) -> int:
    """Move a job's decisions into records/<job>/decisions_attemptN.md and clear them. Returns how many moved."""
    old = con.execute("SELECT page, field_label, value, source, ref, ts FROM decisions WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
    if not old:
        return 0
    d = records.job_dir(job_id)
    n = len(list(d.glob("decisions_attempt*.md"))) + 1
    lines = [f"# Decisions archived from attempt {n}", "", "| Page | Field label | Value | Source | ts |", "|---|---|---|---|---|"]
    lines += [f"| {r['page']} | {r['field_label']} | {str(r['value'] or '').replace('|', '/')[:200]} | {r['source']} | {r['ts']} |" for r in old]
    (d / f"decisions_attempt{n}.md").write_text("\n".join(lines), encoding="utf-8")
    con.execute("DELETE FROM decisions WHERE job_id=?", (job_id,))
    return len(old)


def attempt_history(job_id: int, listing_id: str) -> list[str]:
    con = db.connect()
    out = []
    for r in con.execute("SELECT ts, kind, detail FROM events WHERE (job_id=? AND kind IN ('job:hold','job:ready_for_you','job:budget','job:error','submit:failed','submit:blocked','gate:blocked')) "
                         "OR (job_id IS NULL AND kind LIKE 'hermes:%' AND detail LIKE ?) ORDER BY n DESC LIMIT 12",
                         (job_id, f'%{listing_id}%')):
        d = r["detail"] or ""
        try:
            dd = json.loads(d)
            d = dd.get("reason") or dd.get("why") or dd.get("detail") or dd.get("reasons") or d
        except Exception:
            pass
        line = f"{time.strftime('%m-%d %H:%M', time.localtime(r['ts']))} {r['kind']}: {str(d)[:220]}"
        if "credential" in line.lower() or "bitwarden" in line.lower():
            try:
                from .creds.bitwarden import env_login_active
                if env_login_active():
                    line += " [RESOLVED: the password helper works now (one application password); call `credentials` as normal]"
            except Exception:
                pass
        out.append(line)
    return list(reversed(out))


async def after_hold(job: dict, ctx, ex, reason: str) -> dict | None:
    """What follows a hold: a screenshot and a notification for ordinary holds; for a shadow draft (the gate
    passed, no click) the judge's verdict instead, stored with the job and logged as job:judged."""
    from .judge import should_judge
    job_id = job["id"]
    if not should_judge(reason):
        try:
            png = await ex.screenshot_png()
            records.save_screenshot(job_id, png, "hold")
            await notify(f"Held: {job['company']} — {job['title']}\n{reason[:300]}", png)
        except Exception:
            pass
        return None
    from .judge import judge_page
    return await judge_page(job, ctx, ex, shot="shadow")


async def readback_after_page(ctx, snap, job_id: int, page: int) -> list[str]:
    """Read-back of one page: the decisions made on `page` whose controls are in `snap` are compared with the
    live DOM (no scrolling) and the mismatches logged as gate:readback. Called on the page itself, before the
    click that leaves it (t_click), never after the swap."""
    from . import readback
    try:
        decs = records.decisions(job_id)
        await readback.refresh_values(ctx.ex, snap)
        mism = readback.diff(decs, snap, page)
    except Exception as e:  # noqa: BLE001 - never let the audit break a run
        db.event(job_id, "gate:readback", {"page": page, "error": str(e)[:200]})
        return []
    db.event(job_id, "gate:readback", {"page": page, "mismatches": mism})
    return mism


def set_status(job_id: int, status: str, *, reason: str | None = None, blocked_on: list[str] | None = None,
               tab_target_id: str | None = None, finished: bool = False, extra: dict | None = None) -> None:
    with db.tx() as con:
        con.execute("UPDATE jobs SET status=?, hold_reason=?, blocked_on_question_key=?, tab_target_id=COALESCE(?, tab_target_id), "
                    "updated_at=?, finished_at=CASE WHEN ? THEN ? ELSE finished_at END WHERE id=?",
                    (status, reason, (blocked_on or [None])[0], tab_target_id, db.now(), int(finished), db.now(), job_id))
    db.event(job_id, f"job:{status}", {"reason": reason, "blocked_on": blocked_on, **(extra or {})})


def judge_summary(verdict: dict | None) -> dict | None:
    """The judge's verdict as a finish event carries it (Telegram shows it under the message)."""
    if not verdict:
        return None
    return {"verdict": verdict.get("verdict"), "bucket": verdict.get("bucket"), "notes": str(verdict.get("notes") or "")[:300],
            "unsupported": [str(u)[:160] for u in (verdict.get("unsupported") or [])[:6]]}


async def run_page_bounded(coro, budget, *, margin_s: float | None = None, min_s: float = 60.0, poll_s: float = 15.0):
    """Backstop for anything the per-tool cap does not cover (a model request that never returns): a page run may use
    what is left of the wall budget plus a margin, and at least `min_s` plus the margin. Time spent waiting on Surya or
    an email is not budget time (Budget.waiting), so the deadline moves with it. Raises asyncio.TimeoutError."""
    margin = PAGE_HANG_MARGIN_S if margin_s is None else margin_s
    task = asyncio.ensure_future(coro)
    floor = time.monotonic() + min_s + margin
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=poll_s)
            if done:
                return task.result()
            if budget.remaining_s() + margin <= 0 and time.monotonic() >= floor:
                raise asyncio.TimeoutError()
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 - the cancelled page run's own error is not this one's
                pass


def is_draft_hold(reason: str) -> bool:
    """A hold on a finished draft: shadow mode, or a verified auto-submit that wants Surya's OK."""
    return str(reason or "").startswith(("shadow_mode", "review_needed"))


async def hold_extra(job: dict, ctx, ex, reason: str) -> dict | None:
    """What a draft hold's event carries: the unconfirmed answers and the judge's verdict. A review_needed draft
    was judged by the submit check already (never twice); a shadow draft is judged here."""
    if not is_draft_hold(reason):
        return None
    verdict = getattr(ctx, "verdict", None) if getattr(ctx, "judged", False) else await after_hold(job, ctx, ex, reason)
    extra = finish_extra(int(job["id"]))
    if verdict:
        extra = {**extra, "judge": judge_summary(verdict)}
    return extra


def submitted_extra(job_id: int, ctx) -> dict:
    """What a submitted event carries; an automatic submit says so, with the verdict that cleared it."""
    extra = finish_extra(job_id)
    if getattr(ctx, "auto_submitted", False):
        extra = {**extra, "auto": True, "judge": judge_summary(getattr(ctx, "verdict", None))}
    return extra


def finish_extra(job_id: int) -> dict:
    """What a finish event carries for the review message: the decisions Surya did not confirm himself."""
    try:
        return {"inferred": records.inferred_decisions(job_id)}
    except Exception:
        return {}


READY_TEXT_MAX = 1024   # a Telegram photo caption


def ready_for_you_text(company: str, title: str, reason: str, tab_url: str, novnc_url: str = "") -> str:
    """The ready_for_you ping. The noVNC link (the cloud browser, where Surya clicks the CAPTCHA from his
    phone) comes before the long tab URL and the reason is shortened first, so the caption limit never
    cuts the link off."""
    head = f"One click from done: {company} — {title}"
    link = f"Finish it in the cloud browser: {novnc_url}" if novnc_url else ""
    tab = f"Tab: {tab_url[:300]}" if tab_url else ""
    room = READY_TEXT_MAX - len(head) - len(link) - len(tab) - 3
    return "\n".join(x for x in (head, reason[:max(0, min(300, room))], link, tab) if x)


async def notify(text: str, png: bytes | None = None) -> None:
    if SETTINGS.cloud.enabled:
        return   # Telegram runs on the website's server and reports outcomes from the mirrored events
    try:
        from .human import telegram_bot
        await telegram_bot.notify_owner(text, png)
    except Exception as e:  # notifications are best effort
        log.debug("notify failed: %s", e)


def _helpers():
    mail = creds = None
    try:
        from .mail import gmail as mail  # noqa
        from pathlib import Path
        if not Path(SETTINGS.mail.token_json).exists():
            mail = None
    except Exception:
        mail = None
    try:
        from .creds import bitwarden as creds  # noqa
    except Exception:
        creds = None
    return mail, creds


@dataclass
class RunOutcome:
    status: str
    reason: str = ""


BROWSER_DOWN = "browser_down"
_BROWSER_FAILURE_MARKS = ("connect_over_cdp", "ECONNREFUSED 127.0.0.1", "Browser has been closed",
                          "Target page, context or browser has been closed", "browser has disconnected")


def browser_failure_text(text: str) -> bool:
    return any(m.lower() in (text or "").lower() for m in _BROWSER_FAILURE_MARKS)


def is_browser_failure(e: BaseException) -> bool:
    """Chrome itself unreachable (CDP connect timeout, refused, closed): not the posting's fault."""
    return browser_failure_text(f"{type(e).__name__}: {e}")


def _browser_down(job_id: int, why: str) -> "RunOutcome":
    """Put a job that never reached the browser back in the queue, attempt not counted."""
    with db.tx() as con:
        con.execute("UPDATE jobs SET status=?, hold_reason=NULL, attempts=MAX(attempts-1, 0), updated_at=? WHERE id=?",
                    (states.QUEUED, db.now(), job_id))
    db.event(job_id, "job:requeued", {"reason": "browser_unavailable", "detail": why[:300]})
    return RunOutcome(BROWSER_DOWN, why)


class WorkerControl:
    """Pause / stop requests from the website (through the cloud link, on another thread) and the
    browser circuit breaker. Pause flags live in the meta table so they survive a restart."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.wake = asyncio.Event()
        self.stop_current = asyncio.Event()
        self.stop_mode = ""                 # "stop" (hold it) | "skip" (mark it dead)
        self.current_job_id: int | None = None
        self.browser_ok: bool | None = None
        self.browser_failures = 0
        self.current: dict | None = None    # {"job": ..., "ex": Executor} while a job runs

    # -- called from any thread
    def _call(self, fn) -> None:
        if self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(fn)
        else:
            fn()

    def set_paused(self, paused: bool, *, after_current: bool = False, by: str = "") -> None:
        if after_current:
            db.meta_set("pause_after_current", True)
            db.event(self.current_job_id, "worker:pause_after_current", {"by": by})
        else:
            db.meta_set("worker_paused", bool(paused))
            db.meta_set("pause_after_current", False)
            db.event(None, "worker:paused" if paused else "worker:resumed", {"by": by})
        self._call(self.wake.set)

    def request_stop(self, mode: str, by: str = "") -> int | None:
        """Abandon the running job: mode "stop" holds it (and pauses the worker), "skip" marks it dead."""
        job = self.current_job_id
        if mode == "stop":
            db.meta_set("worker_paused", True)
            db.event(job, "worker:stop_now", {"by": by})
        else:
            db.event(job, "worker:skip_current", {"by": by})
        if job is not None:
            def _go():
                self.stop_mode = mode
                self.stop_current.set()
            self._call(_go)
        self._call(self.wake.set)
        return job

    # -- loop side
    def bind(self) -> None:
        """Fresh asyncio events for the running loop (asyncio events belong to one loop)."""
        loop = asyncio.get_running_loop()
        if self.loop is not loop:
            self.loop = loop
            self.wake = asyncio.Event()
            self.stop_current = asyncio.Event()

    @staticmethod
    def paused() -> bool:
        return bool(db.meta_get("worker_paused", False))


CONTROL = WorkerControl()


SAME_TENANT_HOLD_S = 12 * 3600


def same_tenant_credentials_hold(job: dict, now: int | None = None) -> int | None:
    """Another job on the same portal host held on credentials within SAME_TENANT_HOLD_S: its id, else None.
    Only account portals (Workday, Oracle, iCIMS, SuccessFactors) share one login per host."""
    from urllib.parse import urlparse
    host = (urlparse(str(job.get("url") or "")).hostname or "").lower()
    if not host or not any(h in host for h in ("myworkdayjobs.com", "myworkdaysite.com", "oraclecloud.com", "icims.com",
                                                "successfactors.", "sapsf.")):
        return None
    now = now or db.now()
    rows = db.connect().execute(
        "SELECT j.id, l.url FROM jobs j JOIN listings l ON l.id=j.listing_id WHERE j.id != ? AND j.status=? "
        "AND j.hold_reason LIKE 'credentials_unknown%' AND j.updated_at > ?",
        (int(job["id"]), states.HOLD, now - SAME_TENANT_HOLD_S)).fetchall()
    for r in rows:
        if (urlparse(str(r["url"] or "")).hostname or "").lower() == host:
            return int(r["id"])
    return None


async def run_job(job: dict, *, shadow_override: bool | None = None) -> RunOutcome:
    job_id = int(job["id"])
    portal = job.get("ats") or "other"
    adapter = for_portal(portal)
    apply_url = job.get("apply_url") or adapter.apply_url(job["url"])
    history = attempt_history(job_id, job["listing_id"])
    budget = Budget(job_id, portal, job.get("steps_used") or 0, job.get("wall_ms_used") or 0, job.get("failed_submits") or 0)

    # A tenant whose sign-in just failed for another job is not tried again the same day: each failed sign-in counts
    # toward a lockout, and with no reset email the second run ends the same way (Q2, 2026-10-19).
    other = same_tenant_credentials_hold(job)
    if other:
        reason = (f"credentials_unknown: same portal account as job {other}, held on its sign-in in the last 12 hours; "
                  "retry them together once the account works")
        set_status(job_id, states.HOLD, reason=reason)
        return RunOutcome(states.HOLD, reason)

    # Boards that cap applications (Waymo 5 per 60 days, ...): hold until the window opens again.
    from . import caps
    cap_reason = caps.hold_if_capped(job)
    if cap_reason:
        set_status(job_id, states.HOLD, reason=cap_reason)
        return RunOutcome(states.HOLD, cap_reason)

    # Posting still live? (keyless read APIs; None = unknown)
    try:
        from . import scraper
        live = await asyncio.to_thread(scraper.check_posting_live, portal, job["url"])
    except Exception:
        live = None
    if live is False:
        set_status(job_id, states.DEAD, reason="posting_closed", finished=True)
        return RunOutcome(states.DEAD, "posting_closed")

    if not await chrome.ensure_running():
        return _browser_down(job_id, "CDP endpoint not answering")

    ex = Executor(policy=HostPolicy([job["url"], apply_url]), job_id=job_id)
    CONTROL.current = {"job": job, "ex": ex}          # the chat agent may look at this tab (never operate it)
    mail, creds = _helpers()
    client_job = {**job, "resume_file": None}
    ctx = RunContext(job=client_job, ex=ex, budget=budget, classifier=Classifier(), mail=mail, creds=creds)
    ctx.shadow_only = shadow_override is True
    outcome = RunOutcome(states.HOLD, "unknown")
    tab_id = None
    try:
        await ex.start()
        # every fresh start, not only listings typed SuccessFactors: NetApp's and Phillips 66's career sites ('other')
        # hand over to SuccessFactors only after Apply (2026-10-21)
        if not job.get("tab_target_id"):
            if await clear_sf_session(ex):
                db.event(job_id, "sf:session_cleared", {})
        page = None
        reopened = False                    # the form on screen is not the held one (new tab, re-open or reload)
        if job.get("tab_target_id"):
            page = await ex.attach(job["tab_target_id"])
        if page is None:
            await ex.new_page()
            await ex.goto(apply_url)
            reopened = True
        else:
            await ex.bring_to_front()
            await ex.settle()
            if not ex.policy.allowed(ex.url) or "login" in ex.url.lower():
                await ex.goto(apply_url)   # expired session: re-open and re-fill from the store
                reopened = True
            else:
                # A stale Workday tab shows "Something went wrong ... refresh the page": do that, then re-open the posting.
                from .gate import portal_error
                if portal_error(await ex.page_text(30000)):
                    await ex.page.reload(wait_until="domcontentloaded")
                    await ex.settle()
                    reopened = True
                    if portal_error(await ex.page_text(30000)):
                        await ex.goto(apply_url)
                    db.event(job_id, "tab:reloaded", {"url": ex.url[:200]})
        # A wrapper page that embeds the Greenhouse form in an iframe (careerpuck for Lyft, company career
        # sites): the outline cannot see into a cross-origin frame, so open the embedded form itself.
        if portal == "greenhouse" and "greenhouse.io" not in (ex.url or "").lower():
            try:
                embedded = next((fr.url for fr in ex.page.frames if "greenhouse.io" in (fr.url or "").lower() and "job_app" in fr.url), None)
            except Exception:
                embedded = None
            if embedded:
                log.info("worker: job %s: opening the embedded Greenhouse form %s", job_id, embedded[:120])
                apply_url = embedded
                ex.policy = HostPolicy([job["url"], apply_url])
                await ex.goto(apply_url)
                reopened = True
        tab_id = await ex.target_id()
        with db.tx() as con:
            con.execute("UPDATE jobs SET apply_url=?, tab_target_id=? WHERE id=?", (apply_url, tab_id, job_id))
        released_as_filled = keeps_decisions(job) and not reopened
        if keeps_decisions(job) and reopened:
            # The released draft's tab was gone or reloaded: the form gets filled again, so the old decisions go.
            with db.tx() as con:
                moved = archive_decisions(con, job_id)
            db.event(job_id, "decisions:archived", {"why": "released draft re-opened", "moved": moved})
        png = await ex.screenshot_png()
        records.save_screenshot(job_id, png, "opened")
        # The posting text decides conditional answers (relocation at own expense: Yes only when pay is listed).
        try:
            ctx.posting_text = await ex.page_text(40000)
        except Exception:
            ctx.posting_text = ""
        if not answers.posting_pay(ctx.posting_text):
            from . import scraper
            for u in dict.fromkeys(x for x in (job.get("url"), apply_url) if x):
                ctx.posting_text += "\n" + await asyncio.to_thread(scraper.fetch_posting_text, u)
                if answers.posting_pay(ctx.posting_text):
                    break
        db.event(job_id, "posting:pay", {"pay": answers.posting_pay(ctx.posting_text)})

        # Deterministic prefill of mechanical fields before the model sees the page.
        snap = await snapmod.take(ex)
        ctx.last_snapshot = snap
        pre = await adapter.prefill(ex, snap, client_job, ctx.page_no)
        if client_job.get("resume_file"):
            ctx.resume_file = client_job["resume_file"]
        db.event(job_id, "adapter:prefill", {"set": pre.set, "failed": pre.failed, "skipped_self_id": pre.skipped_self_id, "details": pre.details})

        loop = AgentLoop(ctx)
        note = adapter.opening_note(job.get("attempts", 0) + 1)
        if job.get("cleared_live") and released_as_filled:
            note += ("\nSurya RELEASED this draft for a LIVE submission exactly as filled: the tab still shows the form he approved and its "
                     "decisions are kept. Call snapshot, then call submit at once WITHOUT changing any field (the gate checks it live). Only "
                     "if the gate names blockers, fix exactly those from their answer_keys and call submit again.")
        elif job.get("cleared_live"):
            note += ("\nThis job was RELEASED by Surya for a LIVE submission after he reviewed its shadow draft: the gate passes live. "
                     "Call snapshot; the saved draft usually reopens at its Review page: call submit there. If the gate names blockers, "
                     "re-set those fields from their answer_keys and call submit again.")
        if shadow_override is True:
            note += "\nThis is a SHADOW run: fill everything and call submit; the gate stops before the click."
        pages = 0
        while True:
            pages += 1
            # Backstop for anything the per-tool cap does not cover (a model request that never returns): the page
            # run may take at most what is left of the wall budget plus a margin.
            try:
                res: LoopResult = await run_page_bounded(loop.run_page(history, note), budget)
            except asyncio.TimeoutError:
                raise BudgetExceeded(f"budget_spent: a step hung past the wall budget ({budget.wall_s}s)")
            if res.outcome == "page_done":
                note = f"You are now on page {ctx.page_no}. Call snapshot and fill this page."
                # Deterministic prefill on every page, not only the first (Workday's My Experience is page 2 or later).
                try:
                    await ex.settle(quiet_ms=500)
                    # Workday swaps the step in place: wait until the headings differ from the page just left,
                    # or the prefill verifies the old page (ICF's My Information, 2026-10-18).
                    before = tuple((ctx.last_snapshot.headings if ctx.last_snapshot else []) or [])
                    for _ in range(12):
                        snap = await snapmod.take(ex)
                        if tuple(snap.headings or []) != before or not before:
                            break
                        await asyncio.sleep(0.5)
                    ctx.last_snapshot = snap
                    # (the page read-back runs in t_click BEFORE leaving a page: after the swap the old controls are gone)
                    pre = await adapter.prefill(ex, snap, client_job, ctx.page_no)
                    if client_job.get("resume_file"):
                        ctx.resume_file = client_job["resume_file"]
                    db.event(job_id, "adapter:prefill", {"page": ctx.page_no, "set": pre.set, "failed": pre.failed, "skipped_self_id": pre.skipped_self_id, "details": pre.details})
                    if pre.set or pre.failed:
                        note += f" The harness already set {pre.set} field(s) here" + (f" and failed on {pre.failed}" if pre.failed else "") + "; verify them in the outline."
                except Exception as e:  # noqa: BLE001 - prefill is best effort; the model fills what it missed
                    db.event(job_id, "adapter:prefill:error", {"page": ctx.page_no, "error": str(e)[:300]})
                loop = AgentLoop(ctx, client=loop.client, advisor=loop.advisor)  # fresh conversation per page
                if SETTINGS.budgets.max_pages and pages > SETTINGS.budgets.max_pages:
                    raise BudgetExceeded(f"budget_spent: more than {SETTINGS.budgets.max_pages} form pages")
                continue
            if res.outcome == "submitted":
                app_id = ledger.record_application(job["listing_id"], job["company"], job["title"], job["url"], "goat", signal=res.reason)
                set_status(job_id, states.SUBMITTED, reason=None, finished=True, extra=submitted_extra(job_id, ctx))
                png = await ex.screenshot_png()
                records.save_screenshot(job_id, png, "submitted")
                outcome = RunOutcome(states.SUBMITTED, res.reason)
                await notify(f"Submitted: {job['company']} — {job['title']}\n{res.reason[:200]}", png)
                break
            if res.outcome == "error":
                raise RuntimeError(res.reason)
            break
    except Parked as p:
        reason = p.reason
        tab_id = await ex.target_id() or tab_id   # the tab it ended on (it may have followed a new tab)
        if p.state == "dead":
            # the posting is gone (the park found the portal's "no longer exists" page): closed, never retried
            set_status(job_id, states.DEAD, reason=reason, tab_target_id=tab_id, finished=True)
            outcome = RunOutcome(states.DEAD, reason)
        elif p.state == "ready_for_you":
            set_status(job_id, states.READY_FOR_YOU, reason=reason, tab_target_id=tab_id, extra=finish_extra(job_id))
            outcome = RunOutcome(states.READY_FOR_YOU, reason)
            try:
                png = await ex.screenshot_png()
                records.save_screenshot(job_id, png, "ready_for_you")
                await notify(ready_for_you_text(job["company"], job["title"], reason, ex.url, SETTINGS.human.novnc_url), png)
            except Exception:
                pass
        else:
            shadow = is_draft_hold(reason)
            # A shadow draft is judged BEFORE the hold is announced, so the finish message carries the verdict
            # (the Telegram worker formats it the moment the hold event arrives).
            extra = await hold_extra(job, ctx, ex, reason)
            set_status(job_id, states.HOLD, reason=reason, blocked_on=p.blocked_on, tab_target_id=tab_id, extra=extra)
            outcome = RunOutcome(states.HOLD, reason)
            if not shadow:
                await after_hold(job, ctx, ex, reason)
    except BudgetExceeded as b:
        with db.tx() as con:
            con.execute("UPDATE jobs SET model_tier=model_tier+1 WHERE id=?", (job_id,))
        set_status(job_id, states.HOLD, reason=b.reason, tab_target_id=tab_id)
        db.event(job_id, "job:budget", {"reason": b.reason})
        outcome = RunOutcome(states.HOLD, b.reason)
        await notify(f"Budget spent: {job['company']} — {job['title']}\n{b.reason}")
    except Exception as e:
        if tab_id is None and is_browser_failure(e):
            # Chrome did not answer (no page was opened): the posting is fine, so the job goes back to
            # the queue untouched and the worker loop backs off instead of burning the next job too.
            log.warning("run_job %s: browser not reachable: %s", job_id, str(e)[:200])
            outcome = _browser_down(job_id, f"{type(e).__name__}: {str(e)[:200]}")
            return outcome
        log.exception("run_job %s failed", job_id)
        set_status(job_id, states.HOLD, reason=f"error: {type(e).__name__}: {str(e)[:200]}", tab_target_id=tab_id)
        db.event(job_id, "job:error", {"error": str(e)[:500]})
        outcome = RunOutcome(states.HOLD, f"error: {e}")
    finally:
        CONTROL.current = None
        from . import memory
        memory.note_outcome(job, outcome.status, outcome.reason)
        try:
            records.write_decisions_markdown(job_id, {
                "company": job.get("company"), "title": job.get("title"), "posting": job.get("url"), "application_url": ex.url,
                "portal": portal, "resume_attached": ctx.resume_file, "outcome": outcome.status, "reason": outcome.reason,
                "gate": json.dumps(ctx.gate_result or {}), "config_hash": SETTINGS.config_hash(),
            }, ctx.free_text, [])
        except Exception:
            pass
        # Keep the tab for ready_for_you and recent holds; cap the number of held tabs.
        try:
            await ex.close_other_tabs()
            if outcome.status not in (states.READY_FOR_YOU, states.HOLD) and ex.page:
                await ex.page.close()
            await _prune_held_tabs(ex)
        except Exception:
            pass
        await ex.close()
    return outcome


ACTIVE_TAB_STATES = (states.HOLD, states.READY_FOR_YOU, states.QUEUED, states.FILLING, states.GATED, states.SUBMITTING,
                     states.AWAITING_ANSWER, states.AWAITING_CODE)


async def _prune_held_tabs(ex: Executor) -> None:
    """Keep the newest held tabs and close the rest; then close every page no active job points at (popups and the
    pages of earlier runs): 62 open tabs made connect_over_cdp time out and the browser counted as down (2026-10-21).
    Extension pages stay, and so does one page, so the window never closes."""
    con = db.connect()
    rows = con.execute("SELECT id, tab_target_id FROM jobs WHERE tab_target_id IS NOT NULL AND status IN (?, ?) ORDER BY updated_at DESC",
                       (states.HOLD, states.READY_FOR_YOU)).fetchall()
    keep = {r["tab_target_id"] for r in rows[: SETTINGS.budgets.max_held_tabs]}
    stale = [r for r in rows[SETTINGS.budgets.max_held_tabs:]]
    if not ex._context:
        return
    marks = ",".join("?" * len(ACTIVE_TAB_STATES))
    active = {r["tab_target_id"] for r in con.execute(
        f"SELECT tab_target_id FROM jobs WHERE tab_target_id IS NOT NULL AND status IN ({marks})", ACTIVE_TAB_STATES)}
    stale_ids = {r["tab_target_id"] for r in stale}
    pages = list(ex._context.pages)
    doomed = []
    for p in pages:
        try:
            if (p.url or "").startswith("chrome-extension://"):
                continue
            cdp = await ex._context.new_cdp_session(p)
            tid = (await cdp.send("Target.getTargetInfo")).get("targetInfo", {}).get("targetId")
            await cdp.detach()
            if tid and tid not in keep and (tid in stale_ids or tid not in active):
                doomed.append(p)
        except Exception:
            continue
    if doomed and len(doomed) >= len(pages):
        doomed = doomed[:-1]          # never the last page: the window would close
    for p in doomed:
        try:
            await p.close()
        except Exception:
            continue
    if stale:
        with db.tx() as con:
            for r in stale:
                con.execute("UPDATE jobs SET tab_target_id=NULL WHERE id=?", (r["id"],))


PORTAL_RETRY_AFTER_S = 2 * 3600
PORTAL_RETRY_MAX_ATTEMPTS = 4


def requeue_portal_errors(now: int | None = None) -> int:
    """Holds parked on a portal failure page go back to the queue after a while, in a fresh tab
    (the portal keeps the saved draft). Bounded by attempts so a broken posting stops retrying."""
    now = now or db.now()
    n = 0
    with db.tx() as con:
        rows = con.execute("SELECT id FROM jobs WHERE status=? AND hold_reason LIKE 'portal_error:%' AND updated_at < ? AND attempts < ?",
                           (states.HOLD, now - PORTAL_RETRY_AFTER_S, PORTAL_RETRY_MAX_ATTEMPTS)).fetchall()
        for r in rows:
            con.execute("UPDATE jobs SET status=?, hold_reason=NULL, tab_target_id=NULL, updated_at=? WHERE id=?",
                        (states.QUEUED, now, r["id"]))
            con.execute("INSERT INTO events(job_id,ts,kind,detail) VALUES(?,?,?,?)",
                        (r["id"], now, "job:requeued", json.dumps({"reason": "portal_error retry"})))
            n += 1
    return n


BROWSER_HOLD_RETRY_AFTER_S = 3600
PAGE_HANG_MARGIN_S = 180.0      # a page run may overrun the wall budget by this much before it is cut
BROWSER_HOLD_SWEEP_PER_PASS = 1      # 0 switches the drain off


def requeue_browser_error_holds(now: int | None = None, limit: int = BROWSER_HOLD_SWEEP_PER_PASS) -> int:
    """Holds whose reason is a browser failure ('error: TimeoutError: BrowserType.connect_over_cdp …', written
    before the circuit breaker existed) drain back into the queue: one per pass, only when the queue is idle
    and Chrome answers, once per job, the attempt not counted. 152 such holds from 2026-10-16."""
    if limit <= 0 or CONTROL.browser_ok is False or db.meta_get("worker_paused"):
        return 0
    now = now or db.now()
    n = 0
    with db.tx() as con:
        if con.execute("SELECT 1 FROM jobs WHERE status=? LIMIT 1", (states.QUEUED,)).fetchone():
            return 0
        rows = con.execute("SELECT id, hold_reason, attempts FROM jobs WHERE status=? AND hold_reason LIKE 'error: %' AND updated_at < ? "
                           "AND attempts <= ? ORDER BY updated_at LIMIT 200",
                           (states.HOLD, now - BROWSER_HOLD_RETRY_AFTER_S, SETTINGS.budgets.max_attempts)).fetchall()
        for r in rows:
            if n >= limit:
                break
            if not browser_failure_text(r["hold_reason"] or ""):
                continue
            seen = con.execute("SELECT 1 FROM events WHERE job_id=? AND kind='job:requeued' AND detail LIKE '%browser_error_hold%' LIMIT 1", (r["id"],)).fetchone()
            if seen:
                continue
            con.execute("UPDATE jobs SET status=?, hold_reason=NULL, tab_target_id=NULL, attempts=MAX(attempts-1, 0), updated_at=? WHERE id=?",
                        (states.QUEUED, now, r["id"]))
            con.execute("INSERT INTO events(job_id,ts,kind,detail) VALUES(?,?,?,?)",
                        (r["id"], now, "job:requeued", json.dumps({"reason": "browser_error_hold", "detail": (r["hold_reason"] or "")[:200]})))
            n += 1
    return n


def reconcile_after_restart() -> dict:
    """Jobs left mid-flight by a crash. `submitting` is never retried blindly."""
    out = {"requeued": 0, "submitting_held": 0}
    with db.tx() as con:
        for r in con.execute("SELECT id FROM jobs WHERE status IN (?,?,?,?)",
                             (states.FILLING, states.AWAITING_ANSWER, states.AWAITING_CODE, states.GATED)).fetchall():
            con.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (states.QUEUED, db.now(), r["id"]))
            db.event(r["id"], "job:requeued", {"reason": "restart while in flight"})
            out["requeued"] += 1
        for r in con.execute("SELECT id FROM jobs WHERE status=?", (states.SUBMITTING,)).fetchall():
            con.execute("UPDATE jobs SET status=?, hold_reason=?, updated_at=? WHERE id=?",
                        (states.HOLD, "submit_outcome_unreconciled: crashed between click and record; check the portal or the confirmation email", db.now(), r["id"]))
            db.event(r["id"], "job:hold", {"reason": "submit_outcome_unreconciled"})
            out["submitting_held"] += 1
    return out


async def _wait(events: list[asyncio.Event], timeout: float) -> None:
    waiters = [asyncio.create_task(e.wait()) for e in events]
    try:
        await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters:
            w.cancel()


BROWSER_BACKOFF_S = (60, 120, 300, 600, 900)


async def _run_controlled(job: dict) -> RunOutcome:
    """run_job as a task the website can stop (hold) or skip (dead) while it runs."""
    job_id = int(job["id"])
    CONTROL.bind()
    CONTROL.current_job_id = job_id
    CONTROL.stop_current.clear()
    task = asyncio.create_task(run_job(job))
    stopper = asyncio.create_task(CONTROL.stop_current.wait())
    try:
        await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            return task.result()
        mode = CONTROL.stop_mode or "stop"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("worker: job %s raised while stopping", job_id)
        if mode == "skip":
            set_status(job_id, states.DEAD, reason="skipped: from the website while running", finished=True)
            return RunOutcome(states.DEAD, "skipped")
        set_status(job_id, states.HOLD, reason="manual_review: stopped from the website while running")
        return RunOutcome(states.HOLD, "stopped")
    finally:
        stopper.cancel()
        CONTROL.current_job_id = None
        CONTROL.stop_current.clear()
        CONTROL.stop_mode = ""


async def worker_loop(stop: asyncio.Event, idle_s: float = 20.0) -> None:
    CONTROL.bind()
    log.info("worker: reconcile %s", reconcile_after_restart())
    backoff_until = 0.0
    while not stop.is_set():
        CONTROL.wake.clear()
        if CONTROL.paused():
            await _wait([stop, CONTROL.wake], 30)
            continue
        if time.monotonic() < backoff_until:
            await _wait([stop, CONTROL.wake], min(30.0, backoff_until - time.monotonic()))
            continue
        job = claim_next()
        if job is None:
            await _wait([stop, CONTROL.wake], idle_s)
            continue
        log.info("worker: running job %s %s — %s (%s)", job["id"], job["company"], job["title"], job["ats"])
        t0 = time.monotonic()
        try:
            out = await _run_controlled(job)
        except Exception as e:  # run_job already handles; this is the last line of defence
            log.exception("worker: run_job crashed")
            set_status(int(job["id"]), states.HOLD, reason=f"error: {e}")
            out = RunOutcome(states.HOLD, str(e))
        log.info("worker: job %s -> %s (%s) in %.0fs", job["id"], out.status, out.reason[:120], time.monotonic() - t0)
        if out.status == BROWSER_DOWN:
            CONTROL.browser_failures += 1
            wait_s = BROWSER_BACKOFF_S[min(CONTROL.browser_failures, len(BROWSER_BACKOFF_S)) - 1]
            if CONTROL.browser_ok is not False:
                db.event(None, "browser:down", {"reason": out.reason[:300], "retry_in_s": wait_s})
            CONTROL.browser_ok = False
            backoff_until = time.monotonic() + wait_s
            continue
        if CONTROL.browser_ok is False:
            db.event(None, "browser:up", {"after_failures": CONTROL.browser_failures})
        CONTROL.browser_ok = True
        CONTROL.browser_failures = 0
        if db.meta_get("pause_after_current", False):
            db.meta_set("pause_after_current", False)
            db.meta_set("worker_paused", True)
            db.event(None, "worker:paused", {"by": "pause_after_current"})
