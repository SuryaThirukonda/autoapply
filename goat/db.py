"""One SQLite file in WAL mode, one writer (this process). Synchronous sqlite3 is used on
purpose: every transaction here is short, and a single connection keeps 'save the answer and
release the holds' atomic without an async lock dance."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import SETTINGS

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS listings (
  id TEXT PRIMARY KEY,                 -- SimplifyJobs listing id (or import:<hash>)
  company TEXT, title TEXT, url TEXT, category TEXT,
  locations TEXT, terms TEXT, degrees TEXT, sponsorship TEXT, source TEXT,
  date_posted INTEGER, date_updated INTEGER, first_seen INTEGER, updated_at INTEGER,
  ats TEXT, variant TEXT,
  triage_status TEXT, triage_reason TEXT,
  active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  listing_id TEXT UNIQUE REFERENCES listings(id),
  status TEXT NOT NULL,
  lane TEXT,                            -- no_account | account | ready_for_you
  priority INTEGER DEFAULT 0,           -- higher first; released holds get a bump
  attempts INTEGER DEFAULT 0,
  failed_submits INTEGER DEFAULT 0,
  steps_used INTEGER DEFAULT 0,
  wall_ms_used INTEGER DEFAULT 0,
  model_tier INTEGER DEFAULT 0,         -- 0 executor, 1 executor+advisor, 2 advisor-as-executor
  blocked_on_question_key TEXT,
  hold_reason TEXT,
  apply_url TEXT,
  tab_target_id TEXT,
  resume_variant TEXT,
  created_at INTEGER, updated_at INTEGER, claimed_at INTEGER, finished_at INTEGER,
  last_error TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, priority DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_blocked ON jobs(blocked_on_question_key);

CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  listing_id TEXT,
  canonical_key TEXT UNIQUE,            -- u:<host/path> of the posting
  company TEXT, title TEXT, url TEXT,
  origin TEXT,                          -- goat | hermes | tracker | manual
  submitted_at INTEGER,
  signal TEXT,                          -- the positive confirmation signal
  confirmation_email_id TEXT,
  confirmed_at INTEGER
);
CREATE TABLE IF NOT EXISTS applied_keys (
  key TEXT PRIMARY KEY,                 -- u:<url key> | c:<company>|<title>
  application_id INTEGER REFERENCES applications(id),
  company TEXT, title TEXT, url TEXT, origin TEXT, added_at INTEGER
);

CREATE TABLE IF NOT EXISTS answers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL,                    -- normalized question key, e.g. identity.email
  scope TEXT NOT NULL DEFAULT 'global', -- global | company | ats
  scope_value TEXT NOT NULL DEFAULT '',
  answer TEXT,                          -- NULL = known to have no standing answer (hold if required)
  answer_type TEXT,                     -- text | choice | multi_choice | file | policy
  note TEXT,
  source TEXT,                          -- seed:<file> | telegram | remember | hermes | decision
  source_ref TEXT,                      -- message id / file / job id
  approved INTEGER DEFAULT 1,
  created_at INTEGER,
  superseded_by INTEGER REFERENCES answers(id)
);
CREATE INDEX IF NOT EXISTS answers_key ON answers(key, scope, scope_value, superseded_by);

CREATE TABLE IF NOT EXISTS question_aliases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  alias TEXT NOT NULL,                  -- normalized question text, or JSON list of substring sets
  kind TEXT NOT NULL DEFAULT 'exact',   -- exact | substrings
  key TEXT NOT NULL,
  confidence REAL DEFAULT 1.0,
  approved INTEGER DEFAULT 1,
  source TEXT, created_at INTEGER
);
CREATE INDEX IF NOT EXISTS aliases_alias ON question_aliases(alias);

CREATE TABLE IF NOT EXISTS pending_questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER REFERENCES jobs(id),
  key TEXT NOT NULL,
  question TEXT NOT NULL,
  options TEXT,                         -- JSON list for multiple choice
  field_type TEXT,
  chat_message_id INTEGER,
  asked_at INTEGER, deadline_at INTEGER, answered_at INTEGER,
  answer TEXT
);
CREATE INDEX IF NOT EXISTS pq_msg ON pending_questions(chat_message_id);

CREATE TABLE IF NOT EXISTS credentials_index (
  host TEXT PRIMARY KEY,
  email TEXT,
  bw_item_id TEXT,                      -- never a password
  created_at INTEGER, last_used_at INTEGER
);

CREATE TABLE IF NOT EXISTS events (
  n INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER,
  ts INTEGER,
  kind TEXT,
  detail TEXT,                          -- JSON, redacted
  latency_ms INTEGER,
  model TEXT, tokens_in INTEGER, tokens_cached INTEGER, tokens_out INTEGER, cost_usd REAL
);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id, n);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER, page INTEGER,
  field_label TEXT, value TEXT, source TEXT, ref TEXT, ts INTEGER
);
CREATE INDEX IF NOT EXISTS decisions_job ON decisions(job_id);

CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT,                            -- alias | field_map | code
  title TEXT, body TEXT, evidence TEXT,
  status TEXT DEFAULT 'proposed',       -- proposed | approved | rejected | applied
  created_at INTEGER, decided_at INTEGER
);
"""

_lock = threading.RLock()
_con: sqlite3.Connection | None = None


def now() -> int:
    return int(time.time())


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Process-wide connection. SQLite on a local disk, WAL, one writer."""
    global _con
    with _lock:
        if _con is not None:
            return _con
        p = path or SETTINGS.db_path
        p.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(p), check_same_thread=False, isolation_level=None, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
        con.executescript(SCHEMA)
        _con = con
        return con


def reset_for_tests(path: Path) -> sqlite3.Connection:
    global _con
    with _lock:
        if _con is not None:
            _con.close()
        _con = None
        return connect(path)


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """One transaction. Nest-safe through the RLock; the outermost owns BEGIN/COMMIT."""
    con = connect()
    with _lock:
        outer = not con.in_transaction
        if outer:
            con.execute("BEGIN IMMEDIATE")
        try:
            yield con
            if outer:
                con.execute("COMMIT")
        except Exception:
            if outer and con.in_transaction:
                con.execute("ROLLBACK")
            raise


def meta_get(key: str, default: Any = None) -> Any:
    row = connect().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return default if row is None else json.loads(row["value"])


def meta_set(key: str, value: Any) -> None:
    with tx() as con:
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, json.dumps(value)))


REDACT_KEYS = ("password", "passwd", "secret", "code", "otp", "token", "bw_session", "api_key")


def redact(obj: Any) -> Any:
    """Strip anything that looks like a secret before it reaches events/records."""
    if isinstance(obj, dict):
        return {k: ("[redacted]" if any(s in k.lower() for s in REDACT_KEYS) else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    if isinstance(obj, str) and len(obj) > 4000:
        return obj[:4000] + f"...[{len(obj) - 4000} more]"
    return obj


def event(job_id: int | None, kind: str, detail: Any = None, *, latency_ms: int | None = None,
          model: str | None = None, tokens_in: int = 0, tokens_cached: int = 0,
          tokens_out: int = 0, cost_usd: float = 0.0) -> None:
    with tx() as con:
        con.execute(
            "INSERT INTO events(job_id,ts,kind,detail,latency_ms,model,tokens_in,tokens_cached,tokens_out,cost_usd)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (job_id, now(), kind, json.dumps(redact(detail), default=str) if detail is not None else None,
             latency_ms, model, tokens_in, tokens_cached, tokens_out, cost_usd),
        )
