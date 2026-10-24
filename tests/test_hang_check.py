"""scripts/hang_check.py: the agent sat idle 28 minutes after a model request crashed natively (2026-10-24)."""
import importlib.util
import sqlite3
from pathlib import Path

spec = importlib.util.spec_from_file_location("hang_check", Path(__file__).resolve().parent.parent / "scripts" / "hang_check.py")
hang_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hang_check)


def _con(status, updated, last_event):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE jobs(id INTEGER, status TEXT, updated_at INTEGER)")
    con.execute("CREATE TABLE events(job_id INTEGER, ts INTEGER, kind TEXT)")
    con.execute("INSERT INTO jobs VALUES(557, ?, ?)", (status, updated))
    con.execute("INSERT INTO events VALUES(557, ?, 'adapter:prefill')", (last_event,))
    return con


def test_a_silent_job_mid_run_is_a_hang():
    now = 100_000
    assert hang_check.hung(_con("filling", now - 1700, now - 1600), now=now) == 557


def test_recent_events_or_a_waiting_job_are_no_hang():
    now = 100_000
    assert hang_check.hung(_con("filling", now - 1700, now - 60), now=now) is None
    assert hang_check.hung(_con("awaiting_answer", now - 5000, now - 5000), now=now) is None
    assert hang_check.hung(_con("filling", now - 300, now - 300), now=now) is None


def test_other_jobs_events_do_not_hide_a_hang():
    """the supervisor's own 'job:requeued' events kept the newest event recent while the job in flight was silent"""
    now = 100_000
    con = _con("filling", now - 1700, now - 1600)
    con.execute("INSERT INTO events VALUES(999, ?, 'job:requeued')", (now - 30,))
    assert hang_check.hung(con, now=now) == 557
