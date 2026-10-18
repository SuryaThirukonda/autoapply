"""One asyncio process: scraper, worker, Telegram bot, mail reconciler, watchdog. Started by a
Task Scheduler "At log on" task in the interactive session (never as a service: Session 0 cannot
show a browser). Holds SetThreadExecutionState so the machine does not sleep while running."""
from __future__ import annotations

import asyncio
import ctypes
import logging
import signal
import sys
import time

from . import db, ledger, states
from .config import SETTINGS

log = logging.getLogger("goat")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040


def keep_awake(on: bool) -> None:
    if sys.platform != "win32":
        return
    try:
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED if on else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception as e:  # pragma: no cover
        log.warning("SetThreadExecutionState failed: %s", e)


def setup_logging() -> None:
    logging.basicConfig(level=getattr(logging, SETTINGS.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.INFO)


def due_daily(hour: int, last_day: int | None, lt: time.struct_time) -> bool:
    """True once per day, during the given local hour (the watchdog ticks every minute)."""
    return lt.tm_hour == hour and last_day != lt.tm_yday


async def watchdog(stop: asyncio.Event) -> None:
    from .browser import chrome
    last_digest_day = None
    last_nightly_day = None
    while not stop.is_set():
        try:
            from .worker import requeue_portal_errors
            n = requeue_portal_errors()
            if n:
                log.info("watchdog: requeued %d job(s) parked on a portal error", n)
            from .caps import requeue_reopened
            n = requeue_reopened()
            if n:
                log.info("watchdog: requeued %d job(s) whose application-cap window opened", n)
            ok = await chrome.is_healthy()
            if not ok and SETTINGS.browser.launch_if_missing:
                log.warning("watchdog: Chrome CDP not answering; relaunching")
                await chrome.ensure_running()
            # Daily digest at 18:00 local; the nightly proposals pass at 02:00
            lt = time.localtime()
            if due_daily(18, last_digest_day, lt):
                last_digest_day = lt.tm_yday
                await send_digest()
            if due_daily(2, last_nightly_day, lt):
                last_nightly_day = lt.tm_yday
                from . import nightly
                counts = await asyncio.to_thread(nightly.run_nightly)
                log.info("nightly: %s", counts)
        except Exception as e:
            log.warning("watchdog error: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), 60)
        except asyncio.TimeoutError:
            pass


def digest_text() -> str:
    con = db.connect()
    counts = {r["status"]: r["c"] for r in con.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status")}
    lines = ["goat digest", " ".join(f"{k}={v}" for k, v in sorted(counts.items()))]
    for r in con.execute("SELECT j.id, l.company, l.title, j.hold_reason FROM jobs j JOIN listings l ON l.id=j.listing_id "
                         "WHERE j.status IN (?,?) ORDER BY j.updated_at DESC LIMIT 15", (states.READY_FOR_YOU, states.HOLD)):
        lines.append(f"#{r['id']} {r['company']} — {r['title'][:50]}: {(r['hold_reason'] or '')[:90]}")
    unconfirmed = ledger.unconfirmed_older_than(24 * 3600)
    if unconfirmed:
        lines.append(f"{len(unconfirmed)} submitted application(s) with no confirmation email after a day:")
        lines += [f"  {a['company']} — {a['title'][:50]}" for a in unconfirmed[:10]]
    day_ago = db.now() - 86400
    cost = con.execute("SELECT COALESCE(SUM(cost_usd),0) FROM events WHERE ts>?", (day_ago,)).fetchone()[0]
    lines.append(f"model spend last 24h: ${cost:.2f}")
    return "\n".join(lines)


async def send_digest() -> None:
    if SETTINGS.cloud.enabled:
        db.event(None, "digest", {"text": digest_text()})   # the cloud Telegram worker sends it
        return
    from .human import telegram_bot
    await telegram_bot.notify_owner(digest_text())


async def mail_reconciler(stop: asyncio.Event) -> None:
    """Hourly: match submitted applications against confirmation emails."""
    while not stop.is_set():
        try:
            from pathlib import Path
            if Path(SETTINGS.mail.token_json).exists():
                from .mail import gmail
                for app in ledger.unconfirmed_older_than(60):
                    mid = await asyncio.to_thread(gmail.find_confirmation_email, app["company"], int(app["submitted_at"]) - 300, None)
                    if mid:
                        ledger.mark_confirmed(int(app["id"]), mid)
                        with db.tx() as con:
                            rows = con.execute("SELECT id FROM jobs WHERE listing_id=? AND status=?",
                                               (app["listing_id"], states.SUBMITTED)).fetchall()
                            con.execute("UPDATE jobs SET status=?, updated_at=? WHERE listing_id=? AND status=?",
                                        (states.CONFIRMED, db.now(), app["listing_id"], states.SUBMITTED))
                        for r in rows:
                            db.event(r["id"], "job:confirmed", {"email_id": mid})
                        log.info("confirmed %s — %s via email %s", app["company"], app["title"], mid)
        except Exception as e:
            log.warning("mail reconciler: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), 3600)
        except asyncio.TimeoutError:
            pass


async def main(run_worker: bool = True, run_scraper: bool = True) -> None:
    setup_logging()
    db.connect()
    log.info("goat starting; config hash %s; mode=%s live_portals=%s db=%s", SETTINGS.config_hash(), SETTINGS.mode,
             SETTINGS.live_portals, SETTINGS.db_path)
    db.event(None, "supervisor:start", {"config_hash": SETTINGS.config_hash(), "mode": SETTINGS.mode})
    keep_awake(True)
    stop = asyncio.Event()

    def _stop(*_):
        log.info("stopping")
        stop.set()

    try:
        loop = asyncio.get_running_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(s, _stop)
            except (NotImplementedError, RuntimeError):
                signal.signal(s, _stop)
    except Exception:
        pass

    from . import scraper, worker
    from .dashboard import start_in_thread
    start_in_thread(SETTINGS.dashboard_port)
    tasks = [asyncio.create_task(watchdog(stop), name="watchdog"),
             asyncio.create_task(mail_reconciler(stop), name="mail")]
    link = None
    if SETTINGS.cloud.enabled:
        # RDS is the only link to AWS. Telegram runs on the website's server, so the local bot stays off
        # (two bots polling one token would fight over updates).
        from .cloud import CloudLink
        from .chat.service import ChatService
        tasks.append(asyncio.create_task(ChatService(worker_control=worker.CONTROL).run(stop), name="chat"))
        link = CloudLink()
        link.start(asyncio.get_running_loop())
    else:
        from .human.telegram_bot import TelegramChannel
        tasks.append(asyncio.create_task(TelegramChannel().run(), name="telegram"))
    if run_scraper:
        tasks.append(asyncio.create_task(scraper.scrape_loop(stop), name="scraper"))
    if run_worker:
        tasks.append(asyncio.create_task(worker.worker_loop(stop), name="worker"))
    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        stop.set()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        keep_awake(False)
        db.event(None, "supervisor:stop", {})
        if link is not None:
            await asyncio.to_thread(link.stop, 10.0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
