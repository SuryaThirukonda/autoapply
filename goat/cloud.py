"""The cloud link: the only connection between this PC and AWS is the RDS Postgres database.

The agent keeps working from its local SQLite file (so it runs on through an internet outage) and this
module keeps RDS in step with it, in two directions:

  push      every local event, and the jobs, listings, questions, answers, field fills, applications and
            screenshots those events touch, are mirrored to RDS within a couple of seconds (woken by
            db.event). The website and the Telegram worker on EC2 read only the mirror.
  commands  the website and Telegram never change the agent's rows themselves. They insert a row in
            `commands`; this module applies it with the agent's own code (the question broker, the answer
            store, the worker control) and settles it with a result. LISTEN goat_commands wakes it.

psycopg's async mode cannot run on the Proactor event loop Playwright needs on Windows, so the link runs
in two plain threads with synchronous connections. Local SQLite access goes through goat.db's lock.
The database URL is never logged.
"""
from __future__ import annotations

import io
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import __version__, answers, db, states
from .config import SETTINGS

log = logging.getLogger("goat.cloud")

BATCH = 500
SCREENSHOTS_PER_CYCLE = 8
RECONCILE_RECENT_S = 60          # jobs/applications changed lately, even without an event
RECONCILE_FULL_S = 600           # everything small, every ten minutes
RETRY_S = (2, 5, 10, 30, 60)
ACTIVE = (states.FILLING, states.AWAITING_ANSWER, states.AWAITING_CODE, states.GATED, states.SUBMITTING)


def ts(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    try:
        return datetime.fromtimestamp(float(v), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _list_text(v: Any) -> str:
    """listings.locations / terms are JSON lists in SQLite; the mirror keeps one readable line."""
    if not v:
        return ""
    try:
        x = json.loads(v)
    except (TypeError, json.JSONDecodeError):
        return str(v)
    return "; ".join(str(i) for i in x) if isinstance(x, list) else str(x)


def _detail(v: Any) -> dict:
    if not v:
        return {}
    try:
        x = json.loads(v)
    except (TypeError, json.JSONDecodeError):
        return {"text": str(v)}
    return x if isinstance(x, dict) else {"value": x}


def _describe(kind: str, detail: dict) -> str:
    """One human line per event for the website and the chat assistant."""
    d = detail or {}
    special = {
        "page:done": lambda: f"page {d.get('page', '?')} filled, on to page {d.get('next', '?')}",
        "question:answered": lambda: "got the answer" + (f" (via {d['via']})" if d.get("via") else ""),
        "question:held": lambda: "question held by you",
        "question:declined": lambda: "question held; the job goes on hold",
        "question:timeout": lambda: "no answer within 15 minutes; holding",
        "answer:saved": lambda: f"saved the answer to {d.get('key', '?')}" + (f", released {d['released']} held job(s)" if d.get("released") else ""),
        "answer:derived": lambda: f"worked out \"{d.get('question', '')[:80]}\" from your profile: {d.get('answer', '')[:60]}",
        "listing:added": lambda: f"added {d.get('company', '')}, {d.get('title', '')}".strip(", "),
        "listing:new": lambda: f"new listing: {d.get('company', '')} ({d.get('status', '')})".replace(" ()", ""),
        "browser:down": lambda: "Chrome is not answering; jobs go back to the queue",
        "browser:up": lambda: "Chrome is answering again",
        "worker:paused": lambda: "paused",
        "worker:resumed": lambda: "resumed",
        "worker:pause_after_current": lambda: "pausing after this job",
        "worker:stop_now": lambda: "stopping the current job",
        "worker:skip_current": lambda: "skipping the current job",
        "supervisor:start": lambda: "the agent started",
        "supervisor:stop": lambda: "the agent stopped",
        "queue:reordered": lambda: "queue reordered",
        "posting:pay": lambda: f"posting pay: {d.get('pay') or 'none listed'}",
        "digest": lambda: "daily digest",
        "chat:message": lambda: f"Surya: {str(d.get('text', ''))[:160]}",
        "chat:reply": lambda: f"goat: {str(d.get('text', ''))[:160]}",
    }
    if kind in special:
        try:
            return special[kind]()[:300]
        except Exception:
            return kind
    try:
        from .dashboard import _describe as describe
        return describe({"kind": kind, "detail": detail})[:300]
    except Exception:
        return kind


def question_status(row) -> str:
    if row["answered_at"] is None:
        return "open"
    return "declined" if row["answer"] is None else "answered"


def jpeg(png: bytes, max_w: int) -> tuple[bytes, int, int, str]:
    """A screenshot as a JPEG at most max_w wide (PNG passthrough if Pillow cannot read it)."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(png))
        im = im.convert("RGB")
        if im.width > max_w:
            im = im.resize((max_w, round(im.height * max_w / im.width)))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=70, optimize=True)
        return out.getvalue(), im.width, im.height, "image/jpeg"
    except Exception:
        return png, 0, 0, "image/png"


# --------------------------------------------------------------------------------------- push SQL
UPSERT_LISTING = """
insert into listings (id, company, title, url, category, locations, terms, sponsorship, source, ats, variant,
                      triage_status, triage_reason, active, date_posted, first_seen, updated_at, synced_at)
values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
on conflict (id) do update set company=excluded.company, title=excluded.title, url=excluded.url,
  category=excluded.category, locations=excluded.locations, terms=excluded.terms, sponsorship=excluded.sponsorship,
  source=excluded.source, ats=excluded.ats, variant=excluded.variant, triage_status=excluded.triage_status,
  triage_reason=excluded.triage_reason, active=excluded.active, date_posted=excluded.date_posted,
  first_seen=excluded.first_seen, updated_at=excluded.updated_at, synced_at=now()
"""

UPSERT_JOB = """
insert into jobs (id, listing_id, state, lane, priority, attempts, failed_submits, steps_used, wall_ms_used,
                  model_tier, blocked_on_question_key, hold_reason, apply_url, resume_variant, last_error,
                  created_at, updated_at, claimed_at, finished_at, synced_at)
values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
on conflict (id) do update set listing_id=excluded.listing_id, state=excluded.state, lane=excluded.lane,
  priority=excluded.priority, attempts=excluded.attempts, failed_submits=excluded.failed_submits,
  steps_used=excluded.steps_used, wall_ms_used=excluded.wall_ms_used, model_tier=excluded.model_tier,
  blocked_on_question_key=excluded.blocked_on_question_key, hold_reason=excluded.hold_reason,
  apply_url=excluded.apply_url, resume_variant=excluded.resume_variant, last_error=excluded.last_error,
  created_at=excluded.created_at, updated_at=excluded.updated_at, claimed_at=excluded.claimed_at,
  finished_at=excluded.finished_at, synced_at=now()
"""

UPSERT_QUESTION = """
insert into questions (id, job_id, key, question, options, field_type, asked_at, deadline_at, answered_at,
                       answer, status, synced_at)
values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
on conflict (id) do update set job_id=excluded.job_id, key=excluded.key, question=excluded.question,
  options=excluded.options, field_type=excluded.field_type, asked_at=excluded.asked_at,
  deadline_at=excluded.deadline_at, answered_at=excluded.answered_at, answer=excluded.answer,
  status=excluded.status, synced_at=now()
where (questions.status, questions.answered_at, questions.answer) is distinct from
      (excluded.status, excluded.answered_at, excluded.answer)
   or questions.deadline_at is distinct from excluded.deadline_at
"""

UPSERT_ANSWER = """
insert into answers (id, key, scope, scope_value, answer, answer_type, note, source, source_ref, approved,
                     created_at, superseded_by, synced_at)
values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
on conflict (id) do update set answer=excluded.answer, answer_type=excluded.answer_type, note=excluded.note,
  source=excluded.source, source_ref=excluded.source_ref, approved=excluded.approved,
  superseded_by=excluded.superseded_by, synced_at=now()
"""

UPSERT_ALIAS = """
insert into answer_aliases (id, alias, kind, key, confidence, approved, source, created_at)
values (%s,%s,%s,%s,%s,%s,%s,%s)
on conflict (id) do update set alias=excluded.alias, kind=excluded.kind, key=excluded.key,
  confidence=excluded.confidence, approved=excluded.approved, source=excluded.source
"""

INSERT_FILL = """
insert into field_fills (id, job_id, page, label, value, source, ref, ts) values (%s,%s,%s,%s,%s,%s,%s,%s)
on conflict (id) do nothing
"""

UPSERT_APPLICATION = """
insert into applications (id, listing_id, canonical_key, company, title, url, origin, submitted_at, signal,
                          confirmation_email_id, confirmed_at)
values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
on conflict (id) do update set listing_id=excluded.listing_id, canonical_key=excluded.canonical_key,
  company=excluded.company, title=excluded.title, url=excluded.url, origin=excluded.origin,
  submitted_at=excluded.submitted_at, signal=excluded.signal,
  confirmation_email_id=excluded.confirmation_email_id, confirmed_at=excluded.confirmed_at
"""

INSERT_EVENT = """
insert into events (agent_n, ts, job_id, kind, message, data, latency_ms, model, tokens_in, tokens_cached,
                    tokens_out, cost_usd, source)
values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'agent')
on conflict (agent_n) do nothing
"""

INSERT_SHOT = """
insert into screenshots (job_id, agent_ref, tag, taken_at, mime, width, height, bytes)
values (%s,%s,%s,%s,%s,%s,%s,%s) on conflict (agent_ref) do nothing
"""

UPSERT_STATUS = """
insert into agent_status (agent, started_at, heartbeat_at, mode, live_portals, worker_paused, pause_after_current,
                          current_job_id, browser_ok, version, config_hash, last_error)
values (%s,%s, now(), %s,%s,%s,%s,%s,%s,%s,%s,%s)
on conflict (agent) do update set started_at=excluded.started_at, heartbeat_at=now(), mode=excluded.mode,
  live_portals=excluded.live_portals, worker_paused=excluded.worker_paused,
  pause_after_current=excluded.pause_after_current, current_job_id=excluded.current_job_id,
  browser_ok=excluded.browser_ok, version=excluded.version, config_hash=excluded.config_hash,
  last_error=excluded.last_error
"""


def listing_row(r) -> tuple:
    return (r["id"], r["company"], r["title"], r["url"], r["category"], _list_text(r["locations"]),
            _list_text(r["terms"]), r["sponsorship"], r["source"], r["ats"], r["variant"], r["triage_status"],
            r["triage_reason"], bool(r["active"]) if r["active"] is not None else None, ts(r["date_posted"]),
            ts(r["first_seen"]), ts(r["updated_at"]))


def job_row(r) -> tuple:
    return (r["id"], r["listing_id"], r["status"], r["lane"], r["priority"] or 0, r["attempts"] or 0,
            r["failed_submits"] or 0, r["steps_used"] or 0, r["wall_ms_used"] or 0, r["model_tier"] or 0,
            r["blocked_on_question_key"], r["hold_reason"], r["apply_url"], r["resume_variant"], r["last_error"],
            ts(r["created_at"]), ts(r["updated_at"]), ts(r["claimed_at"]), ts(r["finished_at"]))


def question_row(r) -> tuple:
    try:
        opts = json.loads(r["options"]) if r["options"] else []
    except (TypeError, json.JSONDecodeError):
        opts = []
    return (r["id"], r["job_id"], r["key"], r["question"], Jsonb(opts if isinstance(opts, list) else []),
            r["field_type"], ts(r["asked_at"]), ts(r["deadline_at"]), ts(r["answered_at"]), r["answer"],
            question_status(r))


def answer_row(r) -> tuple:
    return (r["id"], r["key"], r["scope"], r["scope_value"] or "", r["answer"], r["answer_type"], r["note"],
            r["source"], r["source_ref"], bool(r["approved"]), ts(r["created_at"]), r["superseded_by"])


def alias_row(r) -> tuple:
    return (r["id"], r["alias"], r["kind"], r["key"], r["confidence"], bool(r["approved"]), r["source"],
            ts(r["created_at"]))


def fill_row(r) -> tuple:
    return (r["id"], r["job_id"], r["page"], r["field_label"], r["value"], r["source"], r["ref"], ts(r["ts"]))


def application_row(r) -> tuple:
    return (r["id"], r["listing_id"], r["canonical_key"], r["company"], r["title"], r["url"], r["origin"],
            ts(r["submitted_at"]), r["signal"], r["confirmation_email_id"], ts(r["confirmed_at"]))


def event_row(r) -> tuple:
    d = _detail(r["detail"])
    return (r["n"], ts(r["ts"]) or datetime.now(timezone.utc), r["job_id"], r["kind"], _describe(r["kind"], d),
            Jsonb(d), r["latency_ms"], r["model"], r["tokens_in"], r["tokens_cached"], r["tokens_out"],
            r["cost_usd"])


# --------------------------------------------------------------------------------------- the link
class CloudLink:
    def __init__(self, url: str | None = None, agent: str | None = None) -> None:
        self.url = url or SETTINGS.cloud.db_url
        self.agent = agent or SETTINGS.cloud.agent_name
        self.started_at = datetime.now(timezone.utc)
        self.loop = None                       # the asyncio loop of the worker (for wake-ups)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._push_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._push_conn: psycopg.Connection | None = None
        self._cursor_events = 0
        self._cursor_fills = 0
        self._cursor_answers = 0
        self._cursor_aliases = 0
        self._cursor_questions = 0
        self._cursor_apps = 0
        self._synced = False
        self._full_requested = True
        self._last_recent = 0.0
        self._last_full = 0.0
        self._last_heartbeat = 0.0
        self._pushed_files: set[str] = set()
        self._shot_backlog: set[int] = set()
        self._since = 0               # local clock (epoch s) of the last cycle that was sent
        self._next_since = 0
        self.last_error = ""
        self.connected = False

    # ------------------------------------------------------------------ lifecycle
    def start(self, loop=None) -> None:
        self.loop = loop
        db.add_event_listener(self._wake.set)
        from .human.questions import BROKER

        async def send_questions(job_id, qs, png):
            # The question rows reach RDS with the next push; the Telegram worker on EC2 sends them.
            self._wake.set()
            return [None] * len(qs)

        BROKER.register_sender(send_questions)
        for name, fn in (("cloud-push", self._push_thread), ("cloud-commands", self._command_thread)):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        log.info("cloud link: started for agent %s", self.agent)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        db.remove_event_listener(self._wake.set)
        for t in self._threads:
            t.join(timeout)
        try:
            self.push_now()           # last events (supervisor:stop) on the way out
        except Exception:
            pass
        self._close_push()

    def _connect(self, autocommit: bool = False) -> psycopg.Connection:
        return psycopg.connect(self.url, autocommit=autocommit, connect_timeout=15,
                               application_name=f"goat-agent:{self.agent}")

    def _close_push(self) -> None:
        if self._push_conn is not None:
            try:
                self._push_conn.close()
            except Exception:
                pass
        self._push_conn = None

    def _conn(self) -> psycopg.Connection:
        if self._push_conn is None or self._push_conn.closed:
            self._push_conn = self._connect()
            self._synced = False
            self._full_requested = True
        return self._push_conn

    # ------------------------------------------------------------------ push
    def _push_thread(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                self.push_now()
                if not self.connected:
                    log.info("cloud link: connected; mirror in step")
                self.connected = True
                failures = 0
                self.last_error = ""
            except Exception as e:
                self.connected = False
                self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
                self._close_push()
                wait = RETRY_S[min(failures, len(RETRY_S) - 1)]
                failures += 1
                log.warning("cloud link: push failed (%s); retrying in %ss", type(e).__name__, wait)
                self._stop.wait(wait)
                continue
            self._wake.wait(SETTINGS.cloud.push_interval_s)
            self._wake.clear()

    def request_full(self) -> None:
        self._full_requested = True
        self._wake.set()

    def push_now(self) -> dict:
        """One push cycle. Thread-safe; returns what it sent."""
        with self._push_lock:
            conn = self._conn()
            if not self._synced:
                self._load_cursors(conn)
            now = time.monotonic()
            full = self._full_requested or now - self._last_full > RECONCILE_FULL_S
            recent = full or now - self._last_recent > RECONCILE_RECENT_S
            sent = self._push_cycle(conn, full=full, recent=recent)
            if full:
                self._full_requested = False
                self._last_full = now
            if recent:
                self._last_recent = now
            if now - self._last_heartbeat > SETTINGS.cloud.heartbeat_s:
                self._heartbeat(conn)
                self._last_heartbeat = now
            self._synced = True
            return sent

    def _load_cursors(self, conn: psycopg.Connection) -> None:
        """What RDS already holds decides where pushing resumes, so a new or restored database refills itself."""
        with conn.cursor() as cur:
            self._cursor_events = cur.execute("select coalesce(max(agent_n), 0) from events").fetchone()[0]
            self._cursor_fills = cur.execute("select coalesce(max(id), 0) from field_fills").fetchone()[0]
            self._cursor_answers = cur.execute("select coalesce(max(id), 0) from answers").fetchone()[0]
            self._cursor_aliases = cur.execute("select coalesce(max(id), 0) from answer_aliases").fetchone()[0]
            self._cursor_questions = cur.execute("select coalesce(max(id), 0) from questions").fetchone()[0]
            self._cursor_apps = cur.execute("select coalesce(max(id), 0) from applications").fetchone()[0]
            self._pushed_files = {r[0] for r in cur.execute("select agent_ref from screenshots")}
        conn.commit()

    def _read_local(self, full: bool, recent: bool) -> dict:
        """Everything to send this cycle, read under goat.db's lock (no other thread mid-transaction)."""
        con = db.connect()
        out: dict[str, list] = {}
        with db._lock:
            evs = con.execute("SELECT * FROM events WHERE n > ? ORDER BY n LIMIT ?", (self._cursor_events, BATCH)).fetchall()
            out["events"] = evs
            job_ids = {e["job_id"] for e in evs if e["job_id"] is not None}
            # Jobs changed since the last cycle without an event of their own (an answer releasing holds).
            cycle_start = db.now()
            job_ids |= {r["id"] for r in con.execute("SELECT id FROM jobs WHERE updated_at >= ?", (self._since - 2,))}
            self._next_since = cycle_start
            if full:
                job_ids |= {r["id"] for r in con.execute("SELECT id FROM jobs")}
            elif recent:
                since = db.now() - RECONCILE_RECENT_S - 30
                job_ids |= {r["id"] for r in con.execute("SELECT id FROM jobs WHERE updated_at >= ? OR status IN (%s)"
                                                          % ",".join("?" * len(ACTIVE)), (since, *ACTIVE))}
            ids = sorted(job_ids)
            jobs, listings, questions = [], [], []
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                marks = ",".join("?" * len(chunk))
                jobs += con.execute(f"SELECT * FROM jobs WHERE id IN ({marks})", chunk).fetchall()
                questions += con.execute(f"SELECT * FROM pending_questions WHERE job_id IN ({marks})", chunk).fetchall()
            lids = sorted({j["listing_id"] for j in jobs if j["listing_id"]})
            for i in range(0, len(lids), 400):
                chunk = lids[i:i + 400]
                listings += con.execute(f"SELECT * FROM listings WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
            seen_q = {q["id"] for q in questions}
            questions += [q for q in con.execute("SELECT * FROM pending_questions WHERE id > ? OR job_id IS NULL",
                                                 (self._cursor_questions,)) if q["id"] not in seen_q]
            out["jobs"], out["listings"], out["questions"] = jobs, listings, questions
            out["answers"] = con.execute("SELECT * FROM answers WHERE id > ? OR superseded_by > ? ORDER BY id",
                                         (self._cursor_answers, self._cursor_answers)).fetchall()
            out["aliases"] = con.execute("SELECT * FROM question_aliases WHERE id > ? ORDER BY id", (self._cursor_aliases,)).fetchall()
            out["fills"] = con.execute("SELECT * FROM decisions WHERE id > ? ORDER BY id LIMIT ?", (self._cursor_fills, BATCH * 4)).fetchall()
            if full or recent or any(e["kind"] in ("job:submitted", "job:confirmed") for e in evs):
                out["apps"] = con.execute("SELECT * FROM applications WHERE id > ? OR confirmed_at >= ? OR ?",
                                          (self._cursor_apps, db.now() - 3600, int(full))).fetchall()
            else:
                out["apps"] = []
        out["shot_jobs"] = sorted(job_ids | self._shot_backlog)
        return out

    def _screenshots(self, job_ids: list[int]) -> list[tuple]:
        rows: list[tuple] = []
        root = SETTINGS.records_dir
        for jid in job_ids:
            d = root / str(jid)
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.png")):
                ref = f"{jid}/{f.name}"
                if ref in self._pushed_files:
                    continue
                stem = f.stem.split("_", 1)
                tag = stem[1] if len(stem) > 1 else ""
                try:
                    when = ts(int(stem[0]) / 1000)
                except ValueError:
                    when = ts(f.stat().st_mtime)
                data, w, h, mime = jpeg(f.read_bytes(), SETTINGS.cloud.screenshot_max_w)
                rows.append((jid, ref, tag, when, mime, w, h, data))
                if len(rows) >= SCREENSHOTS_PER_CYCLE:
                    return rows
        return rows

    def _push_cycle(self, conn: psycopg.Connection, *, full: bool, recent: bool) -> dict:
        b = self._read_local(full, recent)
        shots = self._screenshots(b["shot_jobs"])
        sent = {k: len(v) for k, v in b.items() if k != "shot_jobs"}
        sent["screenshots"] = len(shots)
        if not any(sent.values()):
            self._since = self._next_since
            return sent
        with conn.cursor() as cur:
            if b["listings"]:
                cur.executemany(UPSERT_LISTING, [listing_row(r) for r in b["listings"]])
            if b["jobs"]:
                cur.executemany(UPSERT_JOB, [job_row(r) for r in b["jobs"]])
            if b["questions"]:
                cur.executemany(UPSERT_QUESTION, [question_row(r) for r in b["questions"]])
            if b["answers"]:
                cur.executemany(UPSERT_ANSWER, [answer_row(r) for r in b["answers"]])
            if b["aliases"]:
                cur.executemany(UPSERT_ALIAS, [alias_row(r) for r in b["aliases"]])
            if b["fills"]:
                cur.executemany(INSERT_FILL, [fill_row(r) for r in b["fills"]])
            if b["apps"]:
                cur.executemany(UPSERT_APPLICATION, [application_row(r) for r in b["apps"]])
            if shots:
                cur.executemany(INSERT_SHOT, shots)
            # Events last: by the time a page refreshes on one, the rows it describes are already there.
            if b["events"]:
                cur.executemany(INSERT_EVENT, [event_row(r) for r in b["events"]])
        conn.commit()
        self._since = self._next_since
        if b["events"]:
            self._cursor_events = max(self._cursor_events, max(r["n"] for r in b["events"]))
            if len(b["events"]) >= BATCH:
                self._wake.set()      # more to send: go again at once
        if b["fills"]:
            self._cursor_fills = max(self._cursor_fills, max(r["id"] for r in b["fills"]))
        if b["answers"]:
            self._cursor_answers = max(self._cursor_answers, max(r["id"] for r in b["answers"]))
        if b["aliases"]:
            self._cursor_aliases = max(self._cursor_aliases, max(r["id"] for r in b["aliases"]))
        if b["questions"]:
            self._cursor_questions = max(self._cursor_questions, max(r["id"] for r in b["questions"]))
        if b["apps"]:
            self._cursor_apps = max(self._cursor_apps, max(r["id"] for r in b["apps"]))
        self._pushed_files |= {s[1] for s in shots}
        if len(shots) >= SCREENSHOTS_PER_CYCLE:
            self._shot_backlog = set(b["shot_jobs"])   # more files waiting in these job folders
            self._wake.set()
        else:
            self._shot_backlog = set()
        return sent

    def _heartbeat(self, conn: psycopg.Connection) -> None:
        from .worker import CONTROL
        with conn.cursor() as cur:
            cur.execute(UPSERT_STATUS, (
                self.agent, self.started_at, SETTINGS.mode, list(SETTINGS.live_portals),
                bool(db.meta_get("worker_paused", False)), bool(db.meta_get("pause_after_current", False)),
                CONTROL.current_job_id, CONTROL.browser_ok, __version__, SETTINGS.config_hash(),
                self.last_error or None))
        conn.commit()

    # ------------------------------------------------------------------ commands
    def _command_thread(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                with self._connect(autocommit=True) as listen, self._connect(autocommit=True) as work:
                    listen.execute("LISTEN goat_commands")
                    failures = 0
                    while not self._stop.is_set():
                        self.apply_pending(work)
                        for _n in listen.notifies(timeout=SETTINGS.cloud.command_poll_s, stop_after=1):
                            pass
            except Exception as e:
                wait = RETRY_S[min(failures, len(RETRY_S) - 1)]
                failures += 1
                log.warning("cloud link: command listener failed (%s); retrying in %ss", type(e).__name__, wait)
                self._stop.wait(wait)

    def apply_pending(self, conn: psycopg.Connection) -> int:
        """Apply every pending command, oldest first. Each is settled in its own transaction after the
        resulting local change has been pushed, so a page that refreshes on 'command:done' sees it."""
        n = 0
        while not self._stop.is_set():
            with conn.transaction():
                row = conn.execute("select id, kind, payload, source from commands where status = 'pending' "
                                   "order by id limit 1 for update skip locked").fetchone()
                if row is None:
                    return n
                cid, kind, payload, source = row
                status, result, error = "done", None, None
                try:
                    result = self.apply(kind, payload or {}, source)
                    if isinstance(result, dict) and result.get("ok") is False:
                        status = "rejected"
                        error = str(result.get("message") or result.get("reason") or "refused")[:500]
                except Exception as e:
                    log.exception("cloud link: command %s (%s) failed", cid, kind)
                    status, error = "failed", f"{type(e).__name__}: {str(e)[:300]}"
                try:
                    self.push_now()
                except Exception as e:
                    log.warning("cloud link: push after command %s failed: %s", cid, type(e).__name__)
                conn.execute("update commands set status = %s, picked_at = coalesce(picked_at, now()), done_at = now(), "
                             "result = %s, error = %s where id = %s",
                             (status, Jsonb(result) if result is not None else None, error, cid))
            n += 1
        return n

    def apply(self, kind: str, p: dict, source: str) -> dict:
        """One command, through the same code paths the local dashboard and the old Telegram bot use."""
        from .human.questions import BROKER
        from .worker import CONTROL
        by = f"{source}"
        if kind == "answer_question":
            text = str(p.get("answer") or "").strip()
            if not text:
                return {"ok": False, "reason": "empty_reply", "message": "Empty answer."}
            scope = "company" if p.get("scope") == "company" else "global"
            company = ""
            if scope == "company":
                r = db.connect().execute("SELECT l.company FROM pending_questions p JOIN jobs j ON j.id=p.job_id "
                                         "JOIN listings l ON l.id=j.listing_id WHERE p.id=?", (int(p["question_id"]),)).fetchone()
                company = r["company"] if r else ""
                if not company:
                    scope = "global"
            out = BROKER.answer_pending(int(p["question_id"]), text, source=by, scope=scope, scope_value=company)
            if not out.get("ok"):
                out["message"] = "Already answered, or no longer waiting." if out.get("reason") == "no_pending_question" else out.get("reason")
            return out
        if kind == "decline_question":
            out = BROKER.hold_pending(int(p["question_id"]), source=by)
            if not out.get("ok"):
                out["message"] = "Already answered, or no longer waiting."
            return out
        if kind == "save_answer":
            key = str(p.get("key") or "").strip()
            if not key:
                return {"ok": False, "message": "No key."}
            value = p.get("answer")
            value = None if value is None or str(value).strip() == "" else str(value)
            scope = p.get("scope") or "global"
            released = answers.save_answer(key, value, answer_type=p.get("answer_type") or "text", source=by,
                                           scope=scope, scope_value=str(p.get("scope_value") or ""),
                                           note=p.get("note"), aliases=[p["question"]] if p.get("question") else ())
            BROKER.wake_all()
            return {"ok": True, "key": key, "released": released}
        if kind == "add_listing":
            from .intake import add_manual_listing
            return add_manual_listing(str(p.get("url") or ""), str(p.get("company") or ""), str(p.get("title") or ""), source=by)
        if kind == "job_action":
            return job_action([int(x) for x in p.get("job_ids") or []], str(p.get("action") or ""), by)
        if kind == "reorder":
            return reorder([int(x) for x in p.get("job_ids") or []], by)
        if kind == "worker":
            action = str(p.get("action") or "")
            if action == "pause":
                CONTROL.set_paused(True, by=by)
            elif action == "resume":
                CONTROL.set_paused(False, by=by)
            elif action == "pause_after_current":
                CONTROL.set_paused(True, after_current=True, by=by)
            elif action == "stop_now":
                return {"ok": True, "job_id": CONTROL.request_stop("stop", by)}
            elif action == "skip_current":
                job = CONTROL.request_stop("skip", by)
                if job is None:
                    return {"ok": False, "message": "Nothing is running."}
                return {"ok": True, "job_id": job}
            else:
                return {"ok": False, "message": f"unknown worker action {action}"}
            return {"ok": True}
        if kind == "chat":
            from .chat.service import enqueue
            return enqueue(p, source=by)
        if kind == "resync":
            self.request_full()
            return {"ok": True}
        return {"ok": False, "message": f"unknown command {kind}"}


# --------------------------------------------------------------------------------------- job actions
PIN_PRIORITY = 10_000
ORDER_BASE = 1_000


def job_action(job_ids: list[int], action: str, by: str) -> dict:
    """requeue | hold | dead | skip | queue | pin | unpin | release | unrelease, for jobs that are not running right now.
    release: Surya cleared this one draft for a real submission; it re-runs at once and the gate treats it as live
    whatever GOAT_MODE says. Refused for a listing already in the ledger."""
    if action not in ("requeue", "retry", "hold", "dead", "skip", "queue", "pin", "unpin", "release", "unrelease"):
        return {"ok": False, "message": f"unknown action {action}"}
    from . import ledger
    changed, refused = [], []
    now = db.now()
    with db.tx() as con:
        for jid in job_ids:
            row = con.execute("SELECT j.status, j.priority, l.company, l.title, l.url FROM jobs j JOIN listings l ON l.id=j.listing_id WHERE j.id=?", (jid,)).fetchone()
            if row is None:
                refused.append({"job_id": jid, "why": "no such job"})
                continue
            st = row["status"]
            if action == "unrelease":
                con.execute("UPDATE jobs SET cleared_live=0, updated_at=? WHERE id=?", (now, jid))
                changed.append(jid)
                continue
            if st in ACTIVE:
                refused.append({"job_id": jid, "why": "running now: use Stop or Skip on the Now page"})
                continue
            if action == "release":
                if st in (states.SUBMITTED, states.CONFIRMED):
                    refused.append({"job_id": jid, "why": f"already {st}"})
                    continue
                if ledger.already_applied(row["company"] or "", row["title"] or "", row["url"] or ""):
                    refused.append({"job_id": jid, "why": "already in the ledger (applied before)"})
                    continue
                con.execute("UPDATE jobs SET status=?, hold_reason=NULL, blocked_on_question_key=NULL, attempts=0, failed_submits=0, "
                            "steps_used=0, wall_ms_used=0, priority=MAX(priority, 0)+5000, finished_at=NULL, cleared_live=1, updated_at=? WHERE id=?",
                            (states.QUEUED, now, jid))
                changed.append(jid)
                continue
            if action in ("requeue", "retry", "queue"):
                if st in (states.SUBMITTED, states.CONFIRMED):
                    refused.append({"job_id": jid, "why": f"already {st}"})
                    continue
                con.execute("UPDATE jobs SET status=?, hold_reason=NULL, blocked_on_question_key=NULL, attempts=0, failed_submits=0, "
                            "steps_used=0, wall_ms_used=0, priority=MAX(priority, 0)+5, finished_at=NULL, updated_at=? WHERE id=?",
                            (states.QUEUED, now, jid))
            elif action == "hold":
                if st not in (states.QUEUED, states.NEW, states.READY_FOR_YOU):
                    refused.append({"job_id": jid, "why": f"cannot hold a {st} job"})
                    continue
                con.execute("UPDATE jobs SET status=?, hold_reason=?, updated_at=? WHERE id=?",
                            (states.HOLD, f"manual_review: held from the {by}", now, jid))
            elif action in ("dead", "skip"):
                if st in (states.SUBMITTED, states.CONFIRMED, states.DEAD):
                    refused.append({"job_id": jid, "why": f"already {st}"})
                    continue
                con.execute("UPDATE jobs SET status=?, hold_reason=?, updated_at=?, finished_at=? WHERE id=?",
                            (states.DEAD, f"skipped: from the {by}", now, now, jid))
            elif action == "pin":
                top = con.execute("SELECT COALESCE(MAX(priority), 0) FROM jobs WHERE status=?", (states.QUEUED,)).fetchone()[0]
                con.execute("UPDATE jobs SET priority=?, updated_at=? WHERE id=?", (max(top + 1, PIN_PRIORITY), now, jid))
            elif action == "unpin":
                con.execute("UPDATE jobs SET priority=0, updated_at=? WHERE id=?", (now, jid))
            changed.append(jid)
    kind = {"requeue": "requeued", "retry": "requeued", "queue": "requeued", "release": "released", "unrelease": "unreleased"}.get(action, action)
    for jid in changed:
        db.event(jid, f"job:{kind}", {"by": by, "action": action})
    out: dict[str, Any] = {"ok": bool(changed) or not refused, "changed": changed, "refused": refused}
    if refused and not changed:
        out["message"] = refused[0]["why"]
    return out


def reorder(job_ids: list[int], by: str) -> dict:
    """Queued jobs in the order Surya dragged them into: the first one is claimed first."""
    now = db.now()
    n = len(job_ids)
    with db.tx() as con:
        for i, jid in enumerate(job_ids):
            con.execute("UPDATE jobs SET priority=?, updated_at=? WHERE id=? AND status=? AND priority < ?",
                        (ORDER_BASE + n - i, now, jid, states.QUEUED, PIN_PRIORITY))
    db.event(None, "queue:reordered", {"by": by, "job_ids": job_ids[:50]})
    return {"ok": True, "ordered": n}
