"""The chat agent: the part of goat Surya talks to on Telegram (and, later, the website).

One gpt-6-luna conversation per chat, kept in memory as Responses API items (store=False). Each message gets a
stable developer prefix (soul.md, the skills index, the memory snapshot), a fresh STATUS block, the recent
turns, and the message; the model acts through the tools below and answers in plain text. Replies go back as
`chat:reply` events (text, optional screenshot) that the cloud link mirrors and the Telegram worker sends.

Browser access: a running job's tab is the filling agent's (look only); a parked job's tab may be operated.
Nothing here can submit an application or touch a credential."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from .. import db, memory, records, skills, states
from ..config import SETTINGS, assert_allowed

log = logging.getLogger(__name__)

SOUL = (Path(__file__).parent / "soul.md").read_text(encoding="utf-8")
MAX_STEPS = 12
HISTORY_ITEMS = 24

TOOLS: list[dict] = [
    {"type": "function", "name": "status", "strict": True, "description": "Queue counts, the running job, open questions, mode, Chrome and agent state.",
     "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    {"type": "function", "name": "list_jobs", "strict": True,
     "description": "Jobs with company, title, portal, status and hold reason. Filter by status (queued, hold, ready_for_you, filling, awaiting_answer, submitted, dead) and/or a company name fragment.",
     "parameters": {"type": "object", "properties": {"status": {"type": ["string", "null"]}, "company": {"type": ["string", "null"]}, "limit": {"type": "integer"}},
                    "required": ["status", "company", "limit"], "additionalProperties": False}},
    {"type": "function", "name": "job_detail", "strict": True, "description": "One job: status, reason, attempts, posting URL, open questions, its last 15 notable events and the fields it decided.",
     "parameters": {"type": "object", "properties": {"job_id": {"type": "integer"}}, "required": ["job_id"], "additionalProperties": False}},
    {"type": "function", "name": "job_action", "strict": True,
     "description": "run_now (queue it first in line), requeue, hold, skip (close it), release (Surya cleared this reviewed draft for a REAL submission: it re-runs at once and submits even in shadow mode; only on his explicit word), unrelease (stand it down), for jobs that are not running right now.",
     "parameters": {"type": "object", "properties": {"job_ids": {"type": "array", "items": {"type": "integer"}}, "action": {"type": "string", "enum": ["run_now", "requeue", "hold", "skip", "release", "unrelease"]}},
                    "required": ["job_ids", "action"], "additionalProperties": False}},
    {"type": "function", "name": "worker", "strict": True, "description": "pause / resume the worker, stop_now (hold the running job) or skip_current (close it).",
     "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["pause", "resume", "stop_now", "skip_current"]}}, "required": ["action"], "additionalProperties": False}},
    {"type": "function", "name": "answer_question", "strict": True,
     "description": "Answer an open question in Surya's words (the broker maps it onto the field's choices). question_id from job_detail/status, or job_id for that job's oldest open question.",
     "parameters": {"type": "object", "properties": {"question_id": {"type": ["integer", "null"]}, "job_id": {"type": ["integer", "null"]}, "answer": {"type": "string"}},
                    "required": ["question_id", "job_id", "answer"], "additionalProperties": False}},
    {"type": "function", "name": "keep_as_is", "strict": True,
     "description": "Surya set the asked fields himself in the browser: settle that job's open questions as AS_IS so the filling agent records what the page shows and carries on.",
     "parameters": {"type": "object", "properties": {"job_id": {"type": "integer"}}, "required": ["job_id"], "additionalProperties": False}},
    {"type": "function", "name": "add_listing", "strict": True, "description": "Add a posting URL to the queue (company and title if he gave them).",
     "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "company": {"type": ["string", "null"]}, "title": {"type": ["string", "null"]}},
                    "required": ["url", "company", "title"], "additionalProperties": False}},
    {"type": "function", "name": "screenshot", "strict": True,
     "description": "Picture of a job's page: the running job's tab (look only) or a parked job's held tab. The image is sent to Surya with your reply and shown to you.",
     "parameters": {"type": "object", "properties": {"job_id": {"type": ["integer", "null"], "description": "null = the running job"}}, "required": ["job_id"], "additionalProperties": False}},
    {"type": "function", "name": "page_outline", "strict": True, "description": "The text outline (fields with refs, errors) of a parked job's page, or the running job's page.",
     "parameters": {"type": "object", "properties": {"job_id": {"type": ["integer", "null"]}}, "required": ["job_id"], "additionalProperties": False}},
    {"type": "function", "name": "browser", "strict": True,
     "description": "Operate a PARKED job's page: click a ref or coordinates, type text into the focused field, press a key, scroll, go to a URL on the application's host, or go back. Refused on a running job and on submit controls.",
     "parameters": {"type": "object", "properties": {"job_id": {"type": "integer"},
                                                     "action": {"type": "string", "enum": ["click", "type", "key", "scroll", "goto", "back"]},
                                                     "ref": {"type": ["string", "null"]}, "text": {"type": ["string", "null"]},
                                                     "x": {"type": ["integer", "null"]}, "y": {"type": ["integer", "null"]}, "url": {"type": ["string", "null"]}},
                    "required": ["job_id", "action", "ref", "text", "x", "y", "url"], "additionalProperties": False}},
    {"type": "function", "name": "remember", "strict": True,
     "description": "Save something Surya told you. A fact about HIM for forms: give an answer key (e.g. skills.cpp_years) and the value, plus the question wording as alias. A note about the WORK (a portal quirk, a decision): key null, text only (goes to memory).",
     "parameters": {"type": "object", "properties": {"key": {"type": ["string", "null"]}, "value": {"type": "string"}, "alias": {"type": ["string", "null"]}, "standing": {"type": "boolean", "description": "work notes: true = curated MEMORY.md, false = today's notes"}},
                    "required": ["key", "value", "alias", "standing"], "additionalProperties": False}},
    {"type": "function", "name": "read_skill", "strict": True, "description": "Read one skill (procedure) by name from the SKILLS index.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"], "additionalProperties": False}},
    {"type": "function", "name": "recent_events", "strict": True, "description": "The last N notable events, for one job or across the agent.",
     "parameters": {"type": "object", "properties": {"job_id": {"type": ["integer", "null"]}, "limit": {"type": "integer"}}, "required": ["job_id", "limit"], "additionalProperties": False}},
]

NOTABLE = ("job:claimed", "job:hold", "job:ready_for_you", "job:submitted", "job:confirmed", "job:error", "job:budget", "job:dead", "job:requeued", "job:released",
           "question:asked", "question:answered", "question:timeout", "answer:derived", "answer:auto_affiliation", "gate:blocked", "gate:pass_shadow",
           "gate:pass_live", "park:bounced", "browser:down", "browser:up", "supervisor:start", "supervisor:stop", "worker:paused", "worker:resumed",
           "listing:added", "chat:message", "chat:reply")


@dataclass
class Reply:
    text: str
    screenshot_ref: str | None = None
    job_id: int | None = None


@dataclass
class ChatTurn:
    text: str
    source: str = "telegram"
    message_id: int | None = None
    reply_to_job_id: int | None = None


def _job_row(job_id: int) -> dict | None:
    r = db.connect().execute("SELECT j.*, l.company, l.title, l.url, l.ats FROM jobs j JOIN listings l ON l.id=j.listing_id WHERE j.id=?", (int(job_id),)).fetchone()
    return dict(r) if r else None


def _short(j: dict) -> dict:
    return {"job_id": j["id"], "company": j.get("company"), "title": j.get("title"), "portal": j.get("ats"), "status": j.get("status"),
            "attempts": j.get("attempts"), "reason": (j.get("hold_reason") or "")[:200]}


class ChatAgent:
    def __init__(self, client: AsyncOpenAI | None = None, worker_control: Any = None):
        self.client = client or AsyncOpenAI(api_key=SETTINGS.openai_api_key)
        self.model = SETTINGS.models.classifier            # gpt-6-luna
        self.control = worker_control
        self.history: list[dict] = []
        self.pending_shots: list[tuple[str, int | None]] = []

    # ------------------------------------------------------------------ prompt
    def _prefix(self, turn: ChatTurn) -> list[dict]:
        blocks = [SOUL, skills.index("chat")]
        triggered = skills.for_chat(turn.text)
        if triggered:
            blocks.append("SKILLS IN PLAY:\n" + triggered)
        mem = memory.snapshot(2500)
        if mem:
            blocks.append(mem)
        blocks.append(self._status_text())
        return [{"role": "developer", "content": [{"type": "input_text", "text": "\n\n".join(b for b in blocks if b)}]}]

    def _status_text(self) -> str:
        try:
            st = self.t_status()
        except Exception as e:
            return f"STATUS: unavailable ({type(e).__name__})"
        return "STATUS NOW (live):\n" + json.dumps(st, default=str)[:3000] + f"\nlocal time: {time.strftime('%Y-%m-%d %H:%M')}"

    # ------------------------------------------------------------------ the turn
    async def handle(self, turn: ChatTurn) -> Reply:
        self.pending_shots = []
        db.event(turn.reply_to_job_id, "chat:message", {"text": turn.text[:500], "source": turn.source})
        user_text = turn.text if not turn.reply_to_job_id else f"[replying to the message about job {turn.reply_to_job_id}] {turn.text}"
        self.history.append({"role": "user", "content": [{"type": "input_text", "text": user_text}]})
        self.history = self.history[-HISTORY_ITEMS:]
        items = self._prefix(turn) + list(self.history)
        new_items: list[dict] = []
        final = ""
        for _ in range(MAX_STEPS):
            resp = await self._create(items + new_items)
            calls = [o for o in resp.output if getattr(o, "type", "") == "function_call"]
            texts = [c.text for o in resp.output if getattr(o, "type", "") == "message" for c in getattr(o, "content", []) if getattr(c, "type", "") == "output_text"]
            if texts:
                final = "\n".join(t for t in texts if t).strip()
            if not calls:
                break
            for o in resp.output:
                d = o.model_dump(exclude_none=True) if hasattr(o, "model_dump") else dict(o)
                if d.get("type") in ("function_call", "message", "reasoning"):
                    new_items.append(d)
            for c in calls:
                try:
                    args = json.loads(c.arguments or "{}")
                except Exception:
                    args = {}
                out = await self._dispatch(c.name, args)
                new_items.append({"type": "function_call_output", "call_id": c.call_id, "output": json.dumps(out, default=str)[:12000]})
                if c.name == "screenshot" and out.get("png_b64"):
                    new_items.append({"role": "user", "content": [{"type": "input_text", "text": f"[screenshot of job {out.get('job_id')}]"},
                                                                  {"type": "input_image", "image_url": f"data:image/png;base64,{out['png_b64']}", "detail": "low"}]})
                    out.pop("png_b64", None)
        if not final:
            final = "Done." if new_items else "I did not catch that. Tell me a company, a job id, or what to do."
        self.history.append({"role": "assistant", "content": [{"type": "output_text", "text": final}]})
        shot = self.pending_shots[-1] if self.pending_shots else (None, None)
        reply = Reply(text=final[:3500], screenshot_ref=shot[0], job_id=shot[1] or turn.reply_to_job_id)
        db.event(reply.job_id, "chat:reply", {"text": reply.text, "screenshot_ref": reply.screenshot_ref,
                                              "in_reply_to": turn.message_id, "source": turn.source})
        return reply

    async def _create(self, items: list[dict]) -> Any:
        kwargs: dict = dict(model=assert_allowed(self.model), input=items, tools=TOOLS, store=False, parallel_tool_calls=False,
                            safety_identifier=SETTINGS.safety_identifier(), reasoning={"effort": "low"}, truncation="auto")
        t0 = time.monotonic()
        resp = await self.client.responses.create(**kwargs)
        u = getattr(resp, "usage", None)
        db.event(None, "chat:step", {"n_output": len(resp.output)}, latency_ms=int((time.monotonic() - t0) * 1000),
                 model=getattr(resp, "model", self.model), tokens_in=getattr(u, "input_tokens", 0) or 0,
                 tokens_out=getattr(u, "output_tokens", 0) or 0)
        return resp

    async def _dispatch(self, name: str, args: dict) -> dict:
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return {"error": f"no tool {name}"}
        try:
            out = fn(**args) if not asyncio.iscoroutinefunction(fn) else await fn(**args)
            return out if isinstance(out, dict) else {"result": out}
        except Exception as e:
            log.exception("chat tool %s failed", name)
            return {"error": f"{type(e).__name__}: {str(e)[:300]}"}

    # ------------------------------------------------------------------ tools: reading
    def t_status(self) -> dict:
        con = db.connect()
        counts = {r["status"]: r["c"] for r in con.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status")}
        running = [dict(r) for r in con.execute(
            "SELECT j.id, j.status, l.company, l.title, l.ats FROM jobs j JOIN listings l ON l.id=j.listing_id WHERE j.status IN (?,?,?,?,?)",
            (states.FILLING, states.AWAITING_ANSWER, states.AWAITING_CODE, states.GATED, states.SUBMITTING))]
        open_q = [dict(r) for r in con.execute(
            "SELECT p.id AS question_id, p.job_id, p.question, p.options, l.company FROM pending_questions p JOIN jobs j ON j.id=p.job_id "
            "JOIN listings l ON l.id=j.listing_id WHERE p.answered_at IS NULL ORDER BY p.id DESC LIMIT 8")]
        for q in open_q:
            try:
                q["options"] = json.loads(q["options"]) if q.get("options") else None
            except Exception:
                pass
        holds = [dict(r) for r in con.execute(
            "SELECT substr(hold_reason, 1, instr(hold_reason || ':', ':') - 1) AS reason, COUNT(*) c FROM jobs WHERE status='hold' GROUP BY 1 ORDER BY 2 DESC LIMIT 8")]
        paused = bool(db.meta_get("worker_paused", False))
        return {"mode": SETTINGS.mode, "worker_paused": paused, "jobs": counts, "running": running, "open_questions": open_q, "holds_by_reason": holds}

    def t_list_jobs(self, status: str | None, company: str | None, limit: int = 15) -> dict:
        con = db.connect()
        sql = "SELECT j.*, l.company, l.title, l.url, l.ats FROM jobs j JOIN listings l ON l.id=j.listing_id WHERE 1=1"
        params: list = []
        if status:
            sql += " AND j.status=?"; params.append(status)
        if company:
            sql += " AND lower(l.company) LIKE ?"; params.append(f"%{company.lower()}%")
        sql += " ORDER BY j.updated_at DESC LIMIT ?"; params.append(max(1, min(int(limit or 15), 40)))
        return {"jobs": [_short(dict(r)) for r in con.execute(sql, params)]}

    def t_job_detail(self, job_id: int) -> dict:
        j = _job_row(job_id)
        if not j:
            return {"error": f"no job {job_id}"}
        from ..human.questions import BROKER
        con = db.connect()
        evs = [{"ts": time.strftime("%H:%M", time.localtime(r["ts"])), "kind": r["kind"], "detail": (r["detail"] or "")[:200]} for r in con.execute(
            f"SELECT ts, kind, detail FROM events WHERE job_id=? AND kind IN ({','.join('?' * len(NOTABLE))}) ORDER BY ts DESC LIMIT 15", (job_id, *NOTABLE))]
        decisions = []
        try:
            p = records.job_dir(job_id) / "decisions.md"
            if p.exists():
                decisions = p.read_text(encoding="utf-8").splitlines()[-25:]
        except Exception:
            pass
        return {**_short(j), "url": j.get("url"), "apply_url": j.get("apply_url"), "open_questions": BROKER.pending_for_job(job_id),
                "events": evs, "decisions_tail": decisions}

    def t_recent_events(self, job_id: int | None, limit: int = 20) -> dict:
        con = db.connect()
        q = f"SELECT ts, job_id, kind, detail FROM events WHERE kind IN ({','.join('?' * len(NOTABLE))})"
        params: list = list(NOTABLE)
        if job_id:
            q += " AND job_id=?"; params.append(job_id)
        q += " ORDER BY ts DESC LIMIT ?"; params.append(max(1, min(int(limit or 20), 50)))
        return {"events": [{"ts": time.strftime("%H:%M", time.localtime(r["ts"])), "job_id": r["job_id"], "kind": r["kind"], "detail": (r["detail"] or "")[:160]}
                           for r in con.execute(q, params)]}

    def t_read_skill(self, name: str) -> dict:
        s = skills.get(name)
        return {"name": s.name, "body": s.body} if s else {"error": f"no skill {name}", "available": [x.name for x in skills.load_all()]}

    # ------------------------------------------------------------------ tools: acting
    def t_job_action(self, job_ids: list[int], action: str) -> dict:
        from ..cloud import job_action
        act = {"run_now": "requeue", "requeue": "requeue", "hold": "hold", "skip": "skip", "release": "release", "unrelease": "unrelease"}[action]
        out = job_action([int(x) for x in job_ids], act, "telegram chat")
        if action in ("run_now", "release") and out.get("ok", True):
            with db.tx() as con:
                for jid in job_ids:
                    con.execute("UPDATE jobs SET priority=5000, updated_at=? WHERE id=? AND status=?", (db.now(), int(jid), states.QUEUED))
            if self.control is not None:
                try:
                    self.control.wake.set()
                except Exception:
                    pass
        memory.note(f"chat: {action} on job(s) {job_ids}", by="chat")
        return out

    def t_worker(self, action: str) -> dict:
        if self.control is None:
            return {"error": "no worker in this process"}
        if action in ("pause", "resume"):
            self.control.set_paused(action == "pause", by="telegram chat")
        else:
            self.control.request_stop("stop" if action == "stop_now" else "skip", by="telegram chat")
        return {"ok": True, "action": action}

    def t_answer_question(self, question_id: int | None, job_id: int | None, answer: str) -> dict:
        from ..human.questions import BROKER
        if question_id is None and job_id is not None:
            rows = BROKER.pending_for_job(int(job_id))
            if not rows:
                return {"error": f"job {job_id} has no open question"}
            question_id = int(rows[0]["id"])
        if question_id is None:
            return {"error": "give question_id or job_id"}
        return BROKER.answer_pending(int(question_id), answer, source="telegram chat")

    def t_keep_as_is(self, job_id: int) -> dict:
        from ..human.questions import BROKER
        out = BROKER.keep_as_is(int(job_id), source="telegram chat")
        j = _job_row(job_id)
        if j and j.get("status") == states.HOLD:
            out["note"] = "the job is parked: run_now puts it back; the filling agent records his fields with as_is"
        return out

    def t_add_listing(self, url: str, company: str | None, title: str | None) -> dict:
        from ..intake import add_manual_listing
        return add_manual_listing(url, company or "", title or "", source="telegram chat")

    def t_remember(self, key: str | None, value: str, alias: str | None, standing: bool) -> dict:
        if key:
            from .. import answers
            released = answers.save_answer(key.strip(), value.strip(), source="telegram chat", aliases=[alias] if alias else [])
            return {"ok": True, "key": key, "released_jobs": released}
        (memory.remember if standing else memory.note)(value, by="chat")
        return {"ok": True, "saved_to": "MEMORY.md" if standing else "today's notes"}

    # ------------------------------------------------------------------ tools: browser
    async def _executor_for(self, job_id: int | None, *, operate: bool):
        """(executor, job, owned) for a job's tab. The running job's live executor (look only), or a fresh
        attach to a parked job's held tab (closed by the caller)."""
        from ..browser.executor import Executor, HostPolicy
        cur = getattr(self.control, "current", None) if self.control is not None else None
        if job_id is None:
            if not cur or not cur.get("ex"):
                return None, None, False
            if operate:
                raise RuntimeError("that tab belongs to the running job; tell me what to change and I pass it on")
            return cur["ex"], cur.get("job"), False
        j = _job_row(job_id)
        if not j:
            raise RuntimeError(f"no job {job_id}")
        if cur and cur.get("job", {}).get("id") == int(job_id):
            if operate:
                raise RuntimeError("that job is running now; the filling agent owns its tab")
            return cur["ex"], j, False
        if j.get("status") in (states.FILLING, states.AWAITING_ANSWER, states.AWAITING_CODE, states.GATED, states.SUBMITTING):
            raise RuntimeError("that job is running now; the filling agent owns its tab")
        ex = Executor(policy=HostPolicy([j["url"], j.get("apply_url") or j["url"]]), job_id=int(job_id))
        await ex.start()
        page = await ex.attach(j.get("tab_target_id")) if j.get("tab_target_id") else None
        if page is None:
            if not operate:
                await ex.close()
                raise RuntimeError("its tab is gone (closed or Chrome restarted); run_now reopens the application")
            await ex.new_page()
            await ex.goto(j.get("apply_url") or j["url"])
        return ex, j, True

    async def t_screenshot(self, job_id: int | None) -> dict:
        ex, j, owned = await self._executor_for(job_id, operate=False)
        if ex is None:
            return {"error": "nothing is running; give a job id"}
        try:
            png = await ex.screenshot_png()
            jid = int(j["id"]) if j else None
            ref = None
            if jid is not None:
                p = records.save_screenshot(jid, png, "chat")
                ref = p.name
            self.pending_shots.append((ref, jid))
            return {"ok": True, "job_id": jid, "url": ex.url, "screenshot_ref": ref, "png_b64": base64.b64encode(png).decode()}
        finally:
            if owned:
                await ex.close()

    async def t_page_outline(self, job_id: int | None) -> dict:
        from ..browser import snapshot as snapmod
        ex, j, owned = await self._executor_for(job_id, operate=False)
        if ex is None:
            return {"error": "nothing is running; give a job id"}
        try:
            snap = await snapmod.take(ex)
            return {"job_id": j["id"] if j else None, "url": ex.url, "outline": snapmod.render(snap)[:8000]}
        finally:
            if owned:
                await ex.close()

    async def t_browser(self, job_id: int, action: str, ref: str | None, text: str | None, x: int | None, y: int | None, url: str | None) -> dict:
        from ..browser.executor import to_pw_key
        ex, j, owned = await self._executor_for(job_id, operate=True)
        try:
            if action == "click":
                if ref:
                    await ex.click_ref(ref)
                elif x is not None and y is not None:
                    await ex.click_xy(int(x), int(y))
                else:
                    return {"error": "click needs a ref or x,y"}
            elif action == "type":
                if ref:
                    await ex.click_ref(ref)
                await ex.type_text(text or "", real_keys=True, delay_ms=15)
            elif action == "key":
                await ex.page.keyboard.press(to_pw_key(text or "Enter"))
            elif action == "scroll":
                await ex.scroll(int(x or 600), int(y or 400), 0, int(text or 600) if (text or "").lstrip("-").isdigit() else 600)
            elif action == "goto":
                await ex.goto(url or "")
            elif action == "back":
                await ex.page.go_back()
            await asyncio.sleep(0.8)
            png = await ex.screenshot_png()
            p = records.save_screenshot(int(j["id"]), png, f"chat_{action}")
            self.pending_shots.append((p.name, int(j["id"])))
            records.record_decision(int(j["id"]), 0, f"chat {action} {ref or url or text or ''}"[:80], "(by Surya over chat)", "user:manual", ref or "")
            memory.note(f"chat drove the browser on job {j['id']}: {action} {ref or url or ''}", job_id=int(j["id"]), by="chat")
            return {"ok": True, "url": ex.url, "screenshot_ref": p.name, "png_b64": base64.b64encode(png).decode()}
        finally:
            if owned:
                await ex.close()
