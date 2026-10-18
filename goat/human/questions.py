"""Ask the owner a question, keep working, and wait at most ANSWER_WAIT_S measured from the
FIRST ask. No reply -> Parked(hold). Pure asyncio + sqlite: the Telegram bot registers a sender
here and feeds replies back through deliver(); nothing in this module imports Telegram.

Flow (per job):
  keys = await BROKER.ask(job_id, [{"key": ..., "question": ..., "options": [...]}], png)
  ... fill the rest of the page ...
  got = await BROKER.await_answers(job_id, keys)      # dict key -> answer, or raises Parked
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable

from .. import answers, db
from ..budget import Parked
from ..config import SETTINGS

log = logging.getLogger("goat.questions")

# async (job_id, questions, screenshot_png) -> chat message ids, one per question (None = not sent)
Sender = Callable[[int, list[dict], bytes | None], Awaitable[list[int | None]]]

_NUMBERED = re.compile(r"^\s*(\d{1,2})\s*[).:\-]\s*")
_DECLINE_WORDS = {"hold", "hold it", "skip", "-", "pass"}
# "I already filled it out", "done", "leave it as is": Surya set the field himself in the browser (2026-10-18).
_AS_IS = re.compile(r"^(done|all set|already (done|filled|set|entered|selected|answered)|i (already |have )?(filled|did|entered|selected|clicked|set|answered|completed|handled) (it|that|this|them|those|the \w+)( out| in| myself)?|"
                    r"(leave|keep) (it|that|this|them)( as is| as-is| alone)?|as is|as-is|no change|it'?s (done|filled|set))\W*$", re.I)
AS_IS = "AS_IS"


def _answer_type(field_type: str | None, options: list[str] | None) -> str:
    ft = (field_type or "").lower()
    if ft in ("checkbox", "checkboxes", "multi_select", "multiselect", "multi_choice"):
        return "multi_choice"
    if options or ft in ("select", "radio", "choice", "dropdown", "combobox"):
        return "choice"
    if ft in ("file", "upload"):
        return "file"
    return "text"


def _normalize_choice(text: str, options: list[str] | None, question: str = "") -> str:
    """Map a reply onto an option: 1-based index, case-insensitive match, then gpt-6-luna reading the
    reply in Surya's own words ("the first one", "yes as long as it's paid"); else keep the text."""
    t = (text or "").strip()
    if not options:
        return t
    if t.isdigit():
        i = int(t)
        if 1 <= i <= len(options):
            return str(options[i - 1])
    low = answers.norm(t)
    for o in options:
        if answers.norm(str(o)) == low:
            return str(o)
    hit = interpret_reply(question, [str(o) for o in options], t)
    return hit if hit is not None else t


_INTERPRET_SCHEMA = {
    "type": "object",
    "properties": {
        "choice": {"type": ["string", "null"], "description": "one option copied verbatim, or null when the reply does not pick one"},
        "reason": {"type": "string"},
    },
    "required": ["choice", "reason"],
    "additionalProperties": False,
}
_INTERPRET_INSTRUCTIONS = (
    "Surya Thirukonda answered a job-application question in his own words, over chat. Map his reply onto exactly one of the "
    "field's options when the reply clearly picks or implies one (a number, a paraphrase, 'the first one', 'yes', 'none of those'). "
    "Return the option text verbatim. Return null when the reply does not settle on one option, asks for something else, or says to "
    "hold or skip. Never pick an option he did not mean. Return only the JSON asked for."
)


def interpret_reply(question: str, options: list[str], reply: str) -> str | None:
    """Sync (the reply arrives in a thread): gpt-6-luna maps a free-text reply onto one option, or None."""
    if not options or not reply:
        return None
    try:
        from openai import OpenAI
        from ..config import SETTINGS, assert_allowed
        client = OpenAI(api_key=SETTINGS.openai_api_key, timeout=20)
        prompt = f"Question on the form: {question!r}\nOptions: {json.dumps(options)}\nSurya's reply: {reply!r}"
        resp = client.responses.create(model=assert_allowed(SETTINGS.models.classifier), instructions=_INTERPRET_INSTRUCTIONS, input=prompt,
                                       store=False, safety_identifier=SETTINGS.safety_identifier(),
                                       text={"format": {"type": "json_schema", "name": "interpret_reply", "schema": _INTERPRET_SCHEMA, "strict": True}})
        out = json.loads(resp.output_text)
        choice = out.get("choice")
        if choice is None:
            return None
        for o in options:
            if answers.norm(str(o)) == answers.norm(str(choice)):
                log.info("reply %r read as option %r (%s)", reply[:60], o, str(out.get("reason", ""))[:80])
                return str(o)
    except Exception as e:  # never fatal: the raw reply is stored and the agent copes with it
        log.warning("interpret_reply failed: %s", e)
    return None


def split_reply(text: str, n: int) -> list[str | None]:
    """Split one reply covering n questions. One question -> the whole text. Several -> one per
    line, in order; a leading "1)" / "2." picks the slot explicitly. Missing slots are None."""
    if n <= 1:
        t = (text or "").strip()
        return [t if t else None]
    parts: list[str | None] = [None] * n
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _NUMBERED.match(line)
        if m:
            idx = int(m.group(1)) - 1
            rest = line[m.end():].strip()
            if 0 <= idx < n and parts[idx] is None:
                parts[idx] = rest
                continue
            line = rest
        for i in range(n):
            if parts[i] is None:
                parts[i] = line
                break
    return parts


def _row_dict(row) -> dict:
    d = dict(row)
    if d.get("options"):
        try:
            d["options"] = json.loads(d["options"])
        except (TypeError, json.JSONDecodeError):
            d["options"] = None
    return d


class QuestionBroker:
    """One per process. Holds the per-job wake events and the registered sender."""

    def __init__(self) -> None:
        self.arrived: dict[int, asyncio.Event] = {}
        self._sender: Sender | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------ plumbing
    def get_event(self, job_id: int) -> asyncio.Event:
        ev = self.arrived.get(job_id)
        if ev is None:
            ev = self.arrived[job_id] = asyncio.Event()
        return ev

    def register_sender(self, fn: Sender | None) -> None:
        self._sender = fn

    def _remember_loop(self) -> None:
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

    def _wake(self, job_id: int) -> None:
        """Set the job's event. Direct when called on the loop thread (the bot runs in the same
        loop); via call_soon_threadsafe from any other thread."""
        ev = self.get_event(job_id)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            ev.set()
        elif self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(ev.set)
        else:
            ev.set()

    # ------------------------------------------------------------------ asking
    async def ask(self, job_id: int, questions: list[dict], screenshot_png: bytes | None = None) -> list[str]:
        """Record and send the questions that have no stored answer yet. Returns every key passed
        in (answered or not) so the caller can hand the list straight to await_answers()."""
        self._remember_loop()
        wait_s = int(SETTINGS.budgets.answer_wait_s)
        now = db.now()
        keys: list[str] = []
        new_rows: list[tuple[int, dict]] = []
        reused = 0
        with db.tx() as con:
            for q in questions:
                key = q["key"]
                keys.append(key)
                a = answers.get(key)
                if a is not None and a.has_value:
                    continue
                row = con.execute(
                    "SELECT id FROM pending_questions WHERE job_id=? AND key=? AND answered_at IS NULL "
                    "ORDER BY id DESC LIMIT 1", (job_id, key)).fetchone()
                if row:
                    reused += 1
                    continue
                options = q.get("options") or None
                cur = con.execute(
                    "INSERT INTO pending_questions(job_id,key,question,options,field_type,asked_at,deadline_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (job_id, key, q.get("question") or key,
                     json.dumps(list(options)) if options else None,
                     q.get("field_type"), now, now + wait_s))
                new_rows.append((int(cur.lastrowid), q))
        if not new_rows:
            if reused:
                log.debug("job %s: %d question(s) already pending, not re-asked", job_id, reused)
            return keys

        payload = [{"id": pid, "key": q["key"], "question": q.get("question") or q["key"],
                    "options": list(q.get("options") or []) or None,
                    "field_type": q.get("field_type")} for pid, q in new_rows]
        message_ids: list[int | None] = [None] * len(new_rows)
        if self._sender is None:
            log.warning("job %s: %d question(s) recorded but no sender registered (Telegram not "
                        "configured); they will time out after %ss and hold", job_id, len(new_rows), wait_s)
        else:
            try:
                got = list(await self._sender(job_id, payload, screenshot_png))
                for i in range(min(len(got), len(message_ids))):
                    message_ids[i] = got[i]
            except Exception:  # the worker must keep going whatever the bot does
                log.exception("job %s: sending %d question(s) failed", job_id, len(new_rows))
            with db.tx() as con:
                for (pid, _q), mid in zip(new_rows, message_ids):
                    if mid is not None:
                        con.execute("UPDATE pending_questions SET chat_message_id=? WHERE id=?", (int(mid), pid))
        db.event(job_id, "question:asked", {
            "keys": [q["key"] for _p, q in new_rows],
            "pending_ids": [pid for pid, _q in new_rows],
            "chat_message_ids": message_ids,
            "sent": self._sender is not None and any(m is not None for m in message_ids),
            "deadline_at": now + wait_s,
        })
        return keys

    # ------------------------------------------------------------------ waiting
    def _asked_at(self, job_id: int, keys: list[str]) -> float:
        """Earliest first-ask time over these keys (unanswered rows); a key never asked counts as now."""
        con = db.connect()
        now = time.time()
        earliest = now
        for k in keys:
            row = con.execute(
                "SELECT MIN(asked_at) AS a FROM pending_questions WHERE job_id=? AND key=? AND answered_at IS NULL",
                (job_id, k)).fetchone()
            if row is not None and row["a"] is not None:
                earliest = min(earliest, float(row["a"]))
        return earliest

    def _kept_as_is(self, job_id: int, key: str) -> bool:
        row = db.connect().execute(
            "SELECT answer FROM pending_questions WHERE job_id=? AND key=? AND answered_at IS NOT NULL ORDER BY id DESC LIMIT 1",
            (job_id, key)).fetchone()
        return bool(row is not None and row["answer"] == AS_IS)

    def keep_as_is(self, job_id: int, *, source: str = "chat") -> dict:
        """Every open question of this job is settled as AS_IS: Surya set the fields himself. Nothing is stored
        in the answer store; a waiting run wakes and records the page's values with fill_batch as_is."""
        rows = db.connect().execute(
            "SELECT * FROM pending_questions WHERE job_id=? AND answered_at IS NULL ORDER BY id", (int(job_id),)).fetchall()
        if not rows:
            return {"ok": False, "reason": "no_pending_question", "message": f"job {job_id} has no open question"}
        keys = []
        with db.tx() as con:
            for row in rows:
                con.execute("UPDATE pending_questions SET answered_at=?, answer=? WHERE id=?", (db.now(), AS_IS, row["id"]))
                keys.append(row["key"])
        db.event(int(job_id), "question:answered", {"saved": keys, "declined": [], "as_is": True, "via": source})
        self._wake(int(job_id))
        return {"ok": True, "keys": keys, "as_is": True}

    def declined_keys(self, job_id: int, keys: list[str]) -> list[str]:
        """Keys whose latest pending row for this job was declined ('hold') and still has no answer."""
        con = db.connect()
        out = []
        for k in keys:
            row = con.execute(
                "SELECT answered_at, answer FROM pending_questions WHERE job_id=? AND key=? ORDER BY id DESC LIMIT 1",
                (job_id, k)).fetchone()
            if row is not None and row["answered_at"] is not None and row["answer"] is None:
                a = answers.get(k)
                if a is None or not a.has_value:
                    out.append(k)
        return out

    async def await_answers(self, job_id: int, keys: list[str]) -> dict[str, str]:
        """Block until every key has a value. Deadline = first ask + answer_wait_s (wall clock
        against the stored epoch seconds). Raises Parked(hold) on timeout or decline."""
        self._remember_loop()
        keys = list(dict.fromkeys(keys))
        wait_s = int(SETTINGS.budgets.answer_wait_s)
        deadline = self._asked_at(job_id, keys) + wait_s
        ev = self.get_event(job_id)
        while True:
            found: dict[str, str] = {}
            missing: list[str] = []
            for k in keys:
                a = answers.get(k)
                if a is not None and a.has_value:
                    found[k] = str(a.answer)
                elif self._kept_as_is(job_id, k):
                    found[k] = (f"{AS_IS}: Surya set this field himself in the browser. Take a snapshot and record it with "
                                "fill_batch source 'as_is' (value null); never clear or re-set it.")
                else:
                    missing.append(k)
            if not missing:
                return found
            declined = self.declined_keys(job_id, missing)
            if declined:
                db.event(job_id, "question:declined", {"keys": declined})
                raise Parked(f"answer_declined: {', '.join(declined)}", "hold", missing)
            remaining = deadline - time.time()
            if remaining <= 0:
                db.event(job_id, "question:timeout", {"keys": missing, "waited_s": wait_s})
                raise Parked(f"answer_timeout: {', '.join(missing)}", "hold", missing)
            # Wake on the in-process event, or every 20 s to pick up an answer written by another
            # process (the dashboard, `goat remember`) without waiting for the full deadline.
            try:
                await asyncio.wait_for(ev.wait(), min(remaining, 20.0))
            except TimeoutError:
                pass
            ev.clear()

    # ------------------------------------------------------------------ replies
    def _save_row(self, con, row, text: str, source_ref: str, source: str = "telegram",
                  scope: str = "global", scope_value: str = "") -> tuple[str, str, int]:
        d = _row_dict(row)
        options = d.get("options")
        value = _normalize_choice(text, options, d.get("question") or "")
        atype = _answer_type(d.get("field_type"), options)
        released = answers.save_answer(d["key"], value, answer_type=atype, source=source,
                                       source_ref=source_ref, aliases=[d["question"]],
                                       scope=scope, scope_value=scope_value)
        con.execute("UPDATE pending_questions SET answered_at=?, answer=? WHERE id=?",
                    (db.now(), value, d["id"]))
        return d["key"], value, released

    def _decline_row(self, con, row) -> None:
        con.execute("UPDATE pending_questions SET answered_at=?, answer=NULL WHERE id=?", (db.now(), row["id"]))

    def _apply(self, rows, text: str, source_ref: str, from_user_id: int, source: str = "telegram",
               scope: str = "global", scope_value: str = "") -> dict:
        parts = split_reply(text, len(rows))
        saved: list[dict] = []
        declined: list[str] = []
        unanswered: list[str] = []
        released_total = 0
        jobs: set[int] = set()
        with db.tx() as con:
            for row, part in zip(rows, parts):
                if part is None or part == "":
                    unanswered.append(row["key"])
                    continue
                if part.lower() in _DECLINE_WORDS:
                    self._decline_row(con, row)
                    declined.append(row["key"])
                    jobs.add(row["job_id"])
                    continue
                if _AS_IS.match(part.strip()):
                    # he set it himself: no answer is stored; the waiting run records the page's value (fill_batch as_is)
                    con.execute("UPDATE pending_questions SET answered_at=?, answer=? WHERE id=?", (db.now(), AS_IS, row["id"]))
                    saved.append({"key": row["key"], "answer": AS_IS, "released": 0})
                    jobs.add(row["job_id"])
                    continue
                key, value, released = self._save_row(con, row, part, source_ref, source, scope, scope_value)
                saved.append({"key": key, "answer": value, "released": released})
                released_total += released
                jobs.add(row["job_id"])
        for j in jobs:
            db.event(j, "question:answered", {"saved": [s["key"] for s in saved], "declined": declined,
                                              "from_user": from_user_id, "released": released_total,
                                              "via": source, "scope": scope})
            self._wake(j)
        if not saved and not declined:
            return {"ok": False, "reason": "empty_reply", "unanswered": unanswered}
        out: dict[str, Any] = {"ok": True, "saved": saved, "declined": declined, "unanswered": unanswered,
                               "released": released_total, "job_ids": sorted(jobs)}
        if saved:
            out["key"] = saved[0]["key"]
            out["answer"] = saved[0]["answer"]
        return out

    def deliver(self, chat_message_id: int, text: str, from_user_id: int = 0) -> dict:
        """A reply to the chat message that carried one or more questions. Several rows share a
        chat_message_id when they were sent as one batch: the reply is split by line, in order."""
        rows = db.connect().execute(
            "SELECT * FROM pending_questions WHERE chat_message_id=? AND answered_at IS NULL ORDER BY id",
            (int(chat_message_id),)).fetchall()
        if not rows:
            return {"ok": False, "reason": "no_pending_question"}
        return self._apply(rows, text, str(chat_message_id), from_user_id)

    def answer_pending(self, pending_id: int, text: str, from_user_id: int = 0, *, source: str = "telegram",
                       scope: str = "global", scope_value: str = "") -> dict:
        """Answer exactly one pending row by its id (inline button, dashboard, website, cloud Telegram).
        scope "company" saves the answer for that company only (scope_value = the company)."""
        row = db.connect().execute(
            "SELECT * FROM pending_questions WHERE id=? AND answered_at IS NULL", (int(pending_id),)).fetchone()
        if row is None:
            return {"ok": False, "reason": "no_pending_question"}
        ref = str(row["chat_message_id"]) if row["chat_message_id"] is not None else f"pq:{pending_id}"
        return self._apply([row], text, ref, from_user_id, source, scope, scope_value)

    def hold_it(self, chat_message_id: int) -> dict:
        """The owner declined: mark the rows answered with no answer and wake the waiter, which
        parks the job immediately with answer_declined."""
        rows = db.connect().execute(
            "SELECT * FROM pending_questions WHERE chat_message_id=? AND answered_at IS NULL ORDER BY id",
            (int(chat_message_id),)).fetchall()
        if not rows:
            return {"ok": False, "reason": "no_pending_question"}
        return self._decline_rows(rows)

    def hold_pending(self, pending_id: int, *, source: str = "telegram") -> dict:
        row = db.connect().execute(
            "SELECT * FROM pending_questions WHERE id=? AND answered_at IS NULL", (int(pending_id),)).fetchone()
        if row is None:
            return {"ok": False, "reason": "no_pending_question"}
        return self._decline_rows([row], source)

    def wake_all(self) -> None:
        """Wake every waiting job (an answer saved outside a question reply may satisfy any of them)."""
        for j in list(self.arrived):
            self._wake(j)

    def _decline_rows(self, rows, source: str = "telegram") -> dict:
        jobs: set[int] = set()
        keys: list[str] = []
        with db.tx() as con:
            for row in rows:
                self._decline_row(con, row)
                keys.append(row["key"])
                jobs.add(row["job_id"])
        for j in jobs:
            db.event(j, "question:held", {"keys": keys, "via": source})
            self._wake(j)
        return {"ok": True, "keys": keys, "job_ids": sorted(jobs)}

    # ------------------------------------------------------------------ reporting
    def pending_for_job(self, job_id: int) -> list[dict]:
        rows = db.connect().execute(
            "SELECT * FROM pending_questions WHERE job_id=? AND answered_at IS NULL ORDER BY id", (job_id,)).fetchall()
        return [_row_dict(r) for r in rows]

    def pending_count(self) -> int:
        row = db.connect().execute("SELECT COUNT(*) AS n FROM pending_questions WHERE answered_at IS NULL").fetchone()
        return int(row["n"]) if row else 0

    def expire_overdue(self) -> int:
        """Rows past their deadline and still unanswered. They stay as they are (a late reply
        still saves the answer and releases the hold); the digest reports the count."""
        row = db.connect().execute(
            "SELECT COUNT(*) AS n FROM pending_questions WHERE answered_at IS NULL AND deadline_at <= ?",
            (db.now(),)).fetchone()
        return int(row["n"]) if row else 0


BROKER = QuestionBroker()
