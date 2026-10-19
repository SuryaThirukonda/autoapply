"""Local dashboard: one stdlib HTTP server (no extra dependencies) on 127.0.0.1 that reads goat.db and
the records folder. Read-mostly; the only writes are the operator actions requeue / hold / dead.

  python -m goat dashboard [--port 8787]
"""
from __future__ import annotations

import json
import logging
import mimetypes
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import answers, db, states
from .config import SETTINGS

log = logging.getLogger(__name__)
HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")


def _rows(sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in db.connect().execute(sql, args).fetchall()]


def _one(sql: str, args: tuple = ()):
    r = db.connect().execute(sql, args).fetchone()
    return dict(r) if r else None


def _json_detail(rows: list[dict]) -> list[dict]:
    for r in rows:
        d = r.get("detail")
        if isinstance(d, str):
            try:
                r["detail"] = json.loads(d)
            except json.JSONDecodeError:
                pass
    return rows


# ------------------------------------------------------------------ API
def api_overview() -> dict:
    now = db.now()
    counts = {r["status"]: r["c"] for r in _rows("SELECT status, COUNT(*) c FROM jobs GROUP BY status")}
    lanes = {r["lane"]: r["c"] for r in _rows("SELECT lane, COUNT(*) c FROM jobs WHERE status='queued' GROUP BY lane")}
    spend = _one("SELECT COALESCE(SUM(cost_usd),0) d1, (SELECT COALESCE(SUM(cost_usd),0) FROM events WHERE ts>?) d7 FROM events WHERE ts>?",
                 (now - 7 * 86400, now - 86400))
    holds = _rows("SELECT substr(hold_reason,1,instr(hold_reason||':',':')-1) reason, COUNT(*) c FROM jobs WHERE status IN ('hold','ready_for_you') "
                  "AND hold_reason IS NOT NULL GROUP BY 1 ORDER BY c DESC LIMIT 12")
    per_day = _rows("SELECT date(ts,'unixepoch','localtime') day, "
                    "SUM(CASE WHEN kind='job:submitted' THEN 1 ELSE 0 END) submitted, "
                    "SUM(CASE WHEN kind='job:hold' THEN 1 ELSE 0 END) holds, "
                    "SUM(CASE WHEN kind='job:ready_for_you' THEN 1 ELSE 0 END) ready, "
                    "SUM(CASE WHEN kind='job:claimed' THEN 1 ELSE 0 END) runs, "
                    "ROUND(SUM(COALESCE(cost_usd,0)),3) cost FROM events WHERE ts>? GROUP BY 1 ORDER BY 1", (now - 14 * 86400,))
    minutes = _one("SELECT AVG((finished_at-claimed_at)/60.0) avg_min FROM jobs WHERE finished_at IS NOT NULL AND claimed_at IS NOT NULL")
    return {
        "mode": SETTINGS.mode, "live_portals": SETTINGS.live_portals, "lanes_enabled": SETTINGS.lanes_enabled,
        "config_hash": SETTINGS.config_hash(), "models": {"executor": SETTINGS.models.executor, "advisor": SETTINGS.models.advisor,
                                                           "classifier": SETTINGS.models.classifier},
        "jobs": counts, "queued_by_lane": lanes,
        "applications": _one("SELECT COUNT(*) n, SUM(confirmed_at IS NOT NULL) confirmed FROM applications"),
        "pending_questions": _one("SELECT COUNT(*) n FROM pending_questions WHERE answered_at IS NULL")["n"],
        "answers": _one("SELECT COUNT(*) n FROM answers WHERE superseded_by IS NULL AND approved=1")["n"],
        "spend_usd": {"24h": round(spend["d1"], 4), "7d": round(spend["d7"], 4)},
        "holds_by_reason": holds, "per_day": per_day,
        "avg_minutes_per_finished_job": round(minutes["avg_min"], 1) if minutes and minutes["avg_min"] else None,
        "last_event": _one("SELECT ts, kind FROM events ORDER BY n DESC LIMIT 1"),
        "cdp": SETTINGS.browser.cdp_url, "db": str(SETTINGS.db_path), "now": now,
    }


ACTIVE = (states.FILLING, states.AWAITING_ANSWER, states.AWAITING_CODE, states.GATED, states.SUBMITTING)

_ACTION_WORDS = {
    "snapshot": "reading the page", "fill_batch": "filling fields", "choose": "picking an option",
    "click": "clicking", "upload_resume": "attaching the résumé", "lookup_answer": "looking up an answer",
    "ask_user": "asking you a question", "await_answers": "waiting for your answer",
    "guardcheck": "checking wording", "credentials": "signing in", "enter_email_code": "entering an emailed code",
    "open_verification_link": "verifying the account by email", "submit": "running the gate and submitting",
    "park": "parking the job", "zoom": "zooming in",
}


def _describe(ev: dict) -> str:
    kind = ev.get("kind") or ""
    d = ev.get("detail") if isinstance(ev.get("detail"), dict) else {}
    if kind.startswith("tool:"):
        name = kind.split(":", 1)[1].split(":")[0]
        word = _ACTION_WORDS.get(name, name)
        out = d.get("out") if isinstance(d.get("out"), dict) else {}
        extra = out.get("summary") or out.get("status") or out.get("error") or ""
        return f"{word}{' — ' + str(extra)[:70] if extra else ''}"
    if kind == "model:step":
        return "thinking"
    if kind.startswith("gate:"):
        return "gate: " + kind.split(":", 1)[1]
    if kind.startswith("submit:"):
        return "submit: " + kind.split(":", 1)[1]
    if kind == "adapter:prefill":
        return f"prefilled {d.get('set', 0)} known field(s)"
    if kind == "question:asked":
        return "asked you a question"
    if kind.startswith("job:"):
        return kind.split(":", 1)[1] + (f" — {str(d.get('reason'))[:70]}" if d.get("reason") else "")
    if kind.startswith("advisor:"):
        return "consulted the advisor"
    return kind


def api_now() -> dict:
    now = db.now()
    job = _one("SELECT j.*, l.company, l.title, l.ats, l.url FROM jobs j JOIN listings l ON l.id=j.listing_id "
               f"WHERE j.status IN ({','.join('?' for _ in ACTIVE)}) ORDER BY j.updated_at DESC LIMIT 1", ACTIVE)
    feed = _json_detail(_rows(
        "SELECT e.n, e.ts, e.job_id, e.kind, e.detail, e.latency_ms, e.model, e.cost_usd, l.company "
        "FROM events e LEFT JOIN jobs j ON j.id=e.job_id LEFT JOIN listings l ON l.id=j.listing_id "
        "WHERE e.kind NOT LIKE 'hermes:%' ORDER BY e.n DESC LIMIT 25"))
    for f in feed:
        f["what"] = _describe(f)
    current = None
    if job:
        ev = _json_detail(_rows("SELECT n, ts, kind, detail FROM events WHERE job_id=? ORDER BY n DESC LIMIT 12", (job["id"],)))
        cost = _one("SELECT ROUND(COALESCE(SUM(cost_usd),0),4) c, COUNT(*) n FROM events WHERE job_id=? AND model IS NOT NULL", (job["id"],))
        current = {
            "job": job,
            "doing": _describe(ev[0]) if ev else "starting",
            "since": job.get("claimed_at") or job.get("updated_at"),
            "elapsed_s": now - int(job.get("claimed_at") or job.get("updated_at") or now),
            "steps": job.get("steps_used"), "cost_usd": cost["c"], "requests": cost["n"],
            "recent": [{"ts": e["ts"], "what": _describe(e), "kind": e["kind"]} for e in ev],
        }
    questions = _rows(
        "SELECT p.id, p.job_id, p.key, p.question, p.options, p.field_type, p.asked_at, p.deadline_at, l.company, l.title "
        "FROM pending_questions p LEFT JOIN jobs j ON j.id=p.job_id LEFT JOIN listings l ON l.id=j.listing_id "
        "WHERE p.answered_at IS NULL ORDER BY p.id DESC")
    for q in questions:
        try:
            q["options"] = json.loads(q["options"]) if q["options"] else []
        except (json.JSONDecodeError, TypeError):
            q["options"] = []
        q["expired"] = bool(q["deadline_at"] and q["deadline_at"] < now)
    counts = {r["status"]: r["c"] for r in _rows("SELECT status, COUNT(*) c FROM jobs GROUP BY status")}
    last = _one("SELECT ts FROM events ORDER BY n DESC LIMIT 1")
    return {
        "current": current,
        "idle_next": None if current else _one(
            "SELECT j.id, l.company, l.title, l.ats FROM jobs j JOIN listings l ON l.id=j.listing_id "
            "WHERE j.status='queued' ORDER BY j.priority DESC, j.created_at DESC LIMIT 1"),
        "questions": questions,
        "feed": feed,
        "counts": counts,
        "needs_you": (counts.get(states.READY_FOR_YOU, 0) + counts.get(states.HOLD, 0)),
        "spend_24h": _one("SELECT ROUND(COALESCE(SUM(cost_usd),0),4) c, COUNT(*) n FROM events WHERE ts>? AND model IS NOT NULL",
                          (now - 86400,)),
        "mode": SETTINGS.mode, "live_portals": SETTINGS.live_portals, "executor": SETTINGS.models.executor,
        "last_event_age_s": (now - int(last["ts"])) if last else None,
        "novnc_url": SETTINGS.human.novnc_url,
        "now": now,
    }


def _cdp_ok() -> bool:
    import httpx
    try:
        return httpx.get(SETTINGS.browser.cdp_url + "/json/version", timeout=3).status_code == 200
    except Exception:
        return False


def api_healthz() -> tuple[int, dict]:
    """The Docker healthcheck: 200 when Chrome's CDP answers and goat.db is readable, else 503. Reports the
    newest event's age; HEALTH_MAX_EVENT_AGE_S > 0 also fails a run that has gone quiet for that long."""
    now = db.now()
    try:
        last = _one("SELECT ts FROM events ORDER BY n DESC LIMIT 1")
        db_ok = True
    except Exception:
        last, db_ok = None, False
    age = (now - int(last["ts"])) if last else None
    max_age = SETTINGS.health_max_event_age_s
    stale = bool(max_age) and (age is None or age > max_age)
    cdp = _cdp_ok()
    ok = cdp and db_ok and not stale
    return (200 if ok else 503), {"ok": ok, "cdp": cdp, "db": db_ok, "last_event_age_s": age, "stale": stale,
                                  "mode": SETTINGS.mode}


def api_jobs(q: dict) -> list[dict]:
    status = (q.get("status") or [""])[0]
    search = (q.get("q") or [""])[0].lower()
    limit = int((q.get("limit") or ["100"])[0])
    rows = _rows("SELECT j.id, j.status, j.lane, j.priority, j.attempts, j.failed_submits, j.steps_used, j.wall_ms_used, j.hold_reason, "
                 "j.blocked_on_question_key, j.updated_at, j.claimed_at, j.finished_at, l.company, l.title, l.ats, l.url, l.date_posted "
                 "FROM jobs j JOIN listings l ON l.id=j.listing_id WHERE (?='' OR j.status=?) "
                 "ORDER BY CASE j.status WHEN 'filling' THEN 0 WHEN 'awaiting_answer' THEN 1 WHEN 'awaiting_code' THEN 1 WHEN 'gated' THEN 1 "
                 "WHEN 'submitting' THEN 1 WHEN 'ready_for_you' THEN 2 WHEN 'hold' THEN 3 WHEN 'queued' THEN 4 ELSE 5 END, "
                 "j.updated_at DESC, j.priority DESC LIMIT ?", (status, status, limit * 3 if search else limit))
    if search:
        rows = [r for r in rows if search in f"{r['company']} {r['title']} {r['ats']} {r['hold_reason'] or ''}".lower()][:limit]
    return rows


def api_job(job_id: int) -> dict | None:
    job = _one("SELECT j.*, l.company, l.title, l.ats, l.url, l.locations, l.date_posted FROM jobs j JOIN listings l ON l.id=j.listing_id WHERE j.id=?", (job_id,))
    if not job:
        return None
    events = _json_detail(_rows("SELECT n, ts, kind, detail, latency_ms, model, tokens_in, tokens_cached, tokens_out, cost_usd FROM events "
                                "WHERE job_id=? ORDER BY n DESC LIMIT 300", (job_id,)))
    decisions = _rows("SELECT page, field_label, value, source, ref, ts FROM decisions WHERE job_id=? ORDER BY id", (job_id,))
    questions = _rows("SELECT id, key, question, options, asked_at, deadline_at, answered_at, answer FROM pending_questions WHERE job_id=? ORDER BY id", (job_id,))
    rec = SETTINGS.records_dir / str(job_id)
    files = sorted(p.name for p in rec.iterdir()) if rec.exists() else []
    cost = sum((e.get("cost_usd") or 0) for e in events)
    judgements = _rows("SELECT verdict, bucket, notes, model, ts FROM judgements WHERE job_id=? ORDER BY id DESC LIMIT 5", (job_id,))
    return {"job": job, "events": events, "decisions": decisions, "questions": questions, "files": files, "cost_usd": round(cost, 4),
            "judgements": judgements,
            "novnc_url": SETTINGS.human.novnc_url,
            "hermes_history": _json_detail(_rows("SELECT ts, kind, detail FROM events WHERE job_id IS NULL AND kind LIKE 'hermes:%' AND detail LIKE ? ORDER BY n DESC LIMIT 40",
                                                 (f'%{job["listing_id"]}%',)))}


def api_events(q: dict) -> list[dict]:
    limit = int((q.get("limit") or ["200"])[0])
    kind = (q.get("kind") or [""])[0]
    rows = _rows("SELECT e.n, e.ts, e.job_id, e.kind, e.detail, e.latency_ms, e.model, e.tokens_in, e.tokens_cached, e.tokens_out, e.cost_usd, "
                 "l.company, l.title FROM events e LEFT JOIN jobs j ON j.id=e.job_id LEFT JOIN listings l ON l.id=j.listing_id "
                 "WHERE e.kind NOT LIKE 'hermes:%' AND (?='' OR e.kind LIKE ?) ORDER BY e.n DESC LIMIT ?", (kind, kind + "%", limit))
    return _json_detail(rows)


def api_applications() -> list[dict]:
    return _rows("SELECT id, company, title, url, origin, submitted_at, signal, confirmed_at FROM applications ORDER BY submitted_at DESC LIMIT 500")


def api_questions() -> list[dict]:
    return _rows("SELECT p.id, p.job_id, p.key, p.question, p.options, p.asked_at, p.deadline_at, p.answered_at, p.answer, l.company "
                 "FROM pending_questions p LEFT JOIN jobs j ON j.id=p.job_id LEFT JOIN listings l ON l.id=j.listing_id ORDER BY p.id DESC LIMIT 200")


AUTO_SOURCES = ("default_no", "profile", "classifier")
ASKED_SOURCES = ("telegram", "dashboard")


def api_answers(q: dict) -> list[dict]:
    """Every remembered decision. kind: all | asked (Surya answered a question) | auto (settled from the
    profile, default No) | questionnaire | open (no answer yet)."""
    search = (q.get("q") or [""])[0].lower()
    kind = (q.get("kind") or ["all"])[0]
    rows = _rows("SELECT id, key, scope, scope_value, answer, answer_type, source, note, created_at FROM answers "
                 "WHERE superseded_by IS NULL AND approved=1 AND key NOT LIKE 'always_hold.%' ORDER BY created_at DESC, key")
    if kind == "asked":
        rows = [r for r in rows if r["source"] in ASKED_SOURCES]
    elif kind == "auto":
        rows = [r for r in rows if r["source"] in AUTO_SOURCES]
    elif kind == "questionnaire":
        rows = [r for r in rows if r["source"] == "questionnaire"]
    elif kind == "open":
        rows = [r for r in rows if r["answer"] is None]
    asked = {r["key"]: r["question"] for r in _rows("SELECT key, question FROM pending_questions ORDER BY id")}
    for r in rows:
        r["question"] = asked.get(r["key"]) or r["note"] or r["key"].split(".", 1)[-1].replace("_", " ").replace("-", " ")
    if search:
        rows = [r for r in rows if search in f"{r['key']} {r['answer'] or ''} {r['question']} {r['scope_value'] or ''}".lower()]
    return rows[:800]


def edit_answer(body: dict) -> dict:
    """Change a remembered answer (a new version; the old one is kept as superseded) or forget it
    (unapproved, never deleted). The run picks the change up on its next lookup."""
    key = str(body.get("key") or "").strip()
    if not key:
        return {"ok": False, "reason": "no key"}
    scope = str(body.get("scope") or "global")
    scope_value = str(body.get("scope_value") or "")
    cur = _one("SELECT id, answer_type, note FROM answers WHERE key=? AND scope=? AND scope_value=? AND superseded_by IS NULL "
               "ORDER BY id DESC LIMIT 1", (key, scope, scope_value))
    if body.get("forget"):
        if not cur:
            return {"ok": False, "reason": "not found"}
        with db.tx() as con:
            con.execute("UPDATE answers SET approved=0 WHERE id=?", (cur["id"],))
            con.execute("INSERT INTO events(job_id,ts,kind,detail) VALUES(NULL,?,?,?)",
                        (db.now(), "answer:forgotten", json.dumps({"key": key, "scope": scope, "scope_value": scope_value})))
        return {"ok": True, "forgotten": key}
    text = body.get("answer")
    if text is None:
        return {"ok": False, "reason": "no answer"}
    atype = (cur["answer_type"] if cur else None) or "text"
    if atype == "blank" and str(text).strip():
        atype = "text"
    released = answers.save_answer(key, str(text).strip(), answer_type=atype, source="dashboard", scope=scope,
                                   scope_value=scope_value, note=(cur["note"] if cur else None))
    return {"ok": True, "key": key, "released_jobs": released}


ENV_PATH = SETTINGS.config_dir.parent / ".env"
EDITABLE_KEYS = ("OPENAI_API_KEY", "EXECUTOR_MODEL", "EXECUTOR_EFFORT", "ADVISOR_MODEL", "CLASSIFIER_MODEL", "ALLOWED_MODELS",
                 "GOAT_MODE", "LIVE_PORTALS", "LANES_ENABLED", "TELEGRAM_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_CHAT_ID",
                 "BW_EXE", "BW_SESSION", "BW_PASSWORDFILE", "CDP_PORT", "CHROME_USER_DATA_DIR", "BUDGET_STEPS", "ANSWER_WAIT_S",
                 "ANSWER_WAIT_AWAY_S", "AWAY_AFTER_S")
SECRET_KEYS = ("OPENAI_API_KEY", "TELEGRAM_KEY", "TELEGRAM_BOT_TOKEN", "BW_SESSION", "BW_PASSWORDFILE")


def _read_env() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def _mask(v: str) -> str:
    if not v:
        return ""
    return v[:6] + "…" + v[-4:] if len(v) > 14 else "•" * len(v)


def api_settings() -> dict:
    env = _read_env()
    vals = {k: (_mask(env.get(k, "")) if k in SECRET_KEYS else env.get(k, "")) for k in EDITABLE_KEYS}
    vals["_secrets_set"] = {k: bool(env.get(k)) for k in SECRET_KEYS}
    vals["_running"] = {"mode": SETTINGS.mode, "executor": SETTINGS.models.executor, "allowed": SETTINGS.models.allowed,
                        "config_hash": SETTINGS.config_hash(), "note": "values marked (restart) apply after the agent restarts"}
    from pathlib import Path
    vals["_status"] = {
        "gmail_token": Path(SETTINGS.mail.token_json).exists(),
        "gmail_client": Path(SETTINGS.mail.credentials_json).exists(),
        "bitwarden": _bw_status(),
        "chrome_cdp": SETTINGS.browser.cdp_url,
        "telegram": {"token": bool(_telegram_token(env)), "chat_id": _chat_id(env) != 0,
                     "running": bool(SETTINGS.human.telegram_token) and SETTINGS.human.telegram_owner_chat_id != 0},
    }
    return vals


# ------------------------------------------------------------------ Telegram setup (Settings tab)
TELEGRAM_API = "https://api.telegram.org"


def _telegram_token(env: dict[str, str]) -> str:
    return env.get("TELEGRAM_KEY") or env.get("TELEGRAM_BOT_TOKEN") or ""


def _chat_id(env: dict[str, str]) -> int:
    try:
        return int(env.get("TELEGRAM_OWNER_CHAT_ID") or 0)
    except ValueError:
        return 0


def _telegram_call(token: str, method: str, **params) -> dict:
    import httpx
    try:
        r = httpx.post(f"{TELEGRAM_API}/bot{token}/{method}", json=params, timeout=15)
        return r.json()
    except Exception as e:   # never echo the URL: it contains the token
        return {"ok": False, "description": type(e).__name__}


def telegram_detect() -> dict:
    """Find the chat id of whoever messaged the bot and save it as TELEGRAM_OWNER_CHAT_ID (only when
    exactly one private chat has written to it). Reads the token from .env, so it works before a restart."""
    token = _telegram_token(_read_env())
    if not token:
        return {"ok": False, "error": "no bot token yet: paste it in 'Telegram bot token' and Save first"}
    me = _telegram_call(token, "getMe")
    if not me.get("ok"):
        return {"ok": False, "error": f"Telegram rejected the token ({me.get('description', 'unknown error')})"}
    bot = (me.get("result") or {}).get("username", "")
    upd = _telegram_call(token, "getUpdates", limit=50)
    if not upd.get("ok"):
        if "409" in str(upd.get("error_code", "")) or "conflict" in str(upd.get("description", "")).lower():
            return {"ok": False, "bot": bot, "error": "the agent's bot is already running and reading messages; the chat id is set"
                    if _chat_id(_read_env()) else "the agent's bot is reading messages; stop the agent, then press Find again"}
        return {"ok": False, "bot": bot, "error": upd.get("description", "getUpdates failed")}
    chats: dict[int, str] = {}
    for u in upd.get("result") or []:
        m = u.get("message") or u.get("edited_message") or {}
        ch = m.get("chat") or {}
        if ch.get("type") == "private" and ch.get("id"):
            chats[int(ch["id"])] = " ".join(x for x in (ch.get("first_name"), ch.get("username") and "@" + ch["username"]) if x)
    if not chats:
        return {"ok": False, "bot": bot, "error": f"no messages yet: open Telegram, send /start to @{bot}, then press Find again"}
    if len(chats) > 1:
        return {"ok": False, "bot": bot, "chats": [{"id": k, "name": v} for k, v in chats.items()],
                "error": "more than one person messaged the bot: type your chat id into the field and Save"}
    cid, name = next(iter(chats.items()))
    update_settings({"TELEGRAM_OWNER_CHAT_ID": str(cid)})
    return {"ok": True, "bot": bot, "chat_id": cid, "name": name, "saved": True}


def telegram_test() -> dict:
    env = _read_env()
    token, cid = _telegram_token(env), _chat_id(env)
    if not token or not cid:
        return {"ok": False, "error": "set the bot token and chat id first (Find my chat ID fills the id)"}
    r = _telegram_call(token, "sendMessage", chat_id=cid,
                       text="goat: test message from Settings. Questions, ready-for-you jobs and the daily digest will arrive here.")
    return {"ok": bool(r.get("ok")), "error": None if r.get("ok") else r.get("description", "send failed")}


def _bw_status() -> dict:
    try:
        from .creds import bitwarden
        return {"available": True, "status": bitwarden.status()}
    except Exception as e:
        return {"available": False, "status": f"{type(e).__name__}: {str(e)[:120]}"}


def update_settings(body: dict) -> dict:
    """Write allowed keys into .env (masked values are left untouched)."""
    changed = []
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    for k, v in body.items():
        if k not in EDITABLE_KEYS:
            continue
        v = str(v).strip()
        if k in SECRET_KEYS and ("…" in v or v.startswith("•")):
            continue  # masked placeholder came back unchanged
        if k == "ALLOWED_MODELS" and v and v != "gpt-6-luna":
            return {"ok": False, "error": "ALLOWED_MODELS can only be widened from .env by hand; Surya's rule is gpt-6-luna only"}
        found = False
        for i, line in enumerate(lines):
            if line.startswith(k + "="):
                lines[i] = f"{k}={v}"
                found = True
                break
        if not found:
            lines.append(f"{k}={v}")
        changed.append(k)
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    db.event(None, "settings:updated", {"keys": changed})
    return {"ok": True, "changed": changed, "restart_required": True}


def api_usage() -> dict:
    now = db.now()
    def q(sql, args=()):
        return _rows(sql, args)
    by_model = q("SELECT model, COUNT(*) requests, SUM(tokens_in) tokens_in, SUM(tokens_cached) cached, SUM(tokens_out) tokens_out, "
                 "ROUND(SUM(cost_usd),4) cost, ROUND(AVG(latency_ms)) avg_ms FROM events WHERE model IS NOT NULL AND model!='scripted-demo' GROUP BY model")
    windows = {}
    for name, secs in (("1h", 3600), ("24h", 86400), ("7d", 7 * 86400), ("all", 10 ** 10)):
        r = _one("SELECT COUNT(*) requests, COALESCE(SUM(tokens_in),0) tokens_in, COALESCE(SUM(tokens_out),0) tokens_out, ROUND(COALESCE(SUM(cost_usd),0),4) cost "
                 "FROM events WHERE model IS NOT NULL AND model!='scripted-demo' AND ts>?", (now - secs,))
        windows[name] = r
    per_day = q("SELECT date(ts,'unixepoch','localtime') day, COUNT(*) requests, ROUND(SUM(cost_usd),4) cost FROM events "
                "WHERE model IS NOT NULL AND model!='scripted-demo' AND ts>? GROUP BY 1 ORDER BY 1", (now - 30 * 86400,))
    per_job = q("SELECT e.job_id, l.company, l.title, COUNT(*) requests, ROUND(SUM(e.cost_usd),4) cost FROM events e "
                "LEFT JOIN jobs j ON j.id=e.job_id LEFT JOIN listings l ON l.id=j.listing_id WHERE e.model IS NOT NULL AND e.model!='scripted-demo' "
                "GROUP BY e.job_id ORDER BY cost DESC LIMIT 25")
    kinds = q("SELECT kind, COUNT(*) n FROM events WHERE model IS NOT NULL AND model!='scripted-demo' GROUP BY kind ORDER BY n DESC")
    return {"by_model": by_model, "windows": windows, "per_day": per_day, "per_job": per_job, "by_kind": kinds,
            "allowed_models": SETTINGS.models.allowed}


def action(job_id: int, name: str, body: dict) -> dict:
    with db.tx() as con:
        if name == "requeue":
            con.execute("UPDATE jobs SET status=?, hold_reason=NULL, blocked_on_question_key=NULL, attempts=0, failed_submits=0, steps_used=0, "
                        "wall_ms_used=0, priority=priority+5, updated_at=? WHERE id=?", (states.QUEUED, db.now(), job_id))
        elif name == "hold":
            con.execute("UPDATE jobs SET status=?, hold_reason=?, updated_at=? WHERE id=?",
                        (states.HOLD, "manual_review: " + str(body.get("reason") or "dashboard"), db.now(), job_id))
        elif name == "dead":
            con.execute("UPDATE jobs SET status=?, hold_reason=?, updated_at=?, finished_at=? WHERE id=?",
                        (states.DEAD, str(body.get("reason") or "operator"), db.now(), db.now(), job_id))
        elif name in ("release", "unrelease"):
            from .cloud import job_action
            out = job_action([job_id], name, "dashboard")
            return {"ok": bool(out.get("ok")), **({"error": out.get("message")} if not out.get("ok") else {})}
        else:
            return {"ok": False, "error": "unknown action"}
    db.event(job_id, f"job:{name}", {"by": "dashboard", **body})
    return {"ok": True}


def answer_question(pending_id: int, name: str, text: str) -> dict:
    """Answer (or hold) a pending question from the dashboard. Goes through the same broker path as a
    Telegram reply: saves the answer, releases held jobs on that key; a waiting run picks it up on its
    next poll."""
    from .human.questions import BROKER
    if name == "hold":
        return BROKER.hold_pending(pending_id)
    if not text.strip():
        return {"ok": False, "reason": "empty_reply"}
    return BROKER.answer_pending(pending_id, text.strip())


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "goat-dashboard/0.1"

    def log_message(self, fmt, *args):  # quiet
        log.debug(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode())

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        try:
            if p in ("/", "/index.html"):
                return self._send(200, HTML.encode(), "text/html; charset=utf-8")
            if p == "/healthz":
                code, body = api_healthz()
                return self._json(body, code)
            if p == "/api/overview":
                return self._json(api_overview())
            if p == "/api/now":
                return self._json(api_now())
            if p == "/api/jobs":
                return self._json(api_jobs(q))
            m = re.match(r"^/api/job/(\d+)$", p)
            if m:
                j = api_job(int(m.group(1)))
                return self._json(j if j else {"error": "not found"}, 200 if j else 404)
            if p == "/api/events":
                return self._json(api_events(q))
            if p == "/api/applications":
                return self._json(api_applications())
            if p == "/api/questions":
                return self._json(api_questions())
            if p == "/api/answers":
                return self._json(api_answers(q))
            if p == "/api/settings":
                return self._json(api_settings())
            if p == "/api/usage":
                return self._json(api_usage())
            m = re.match(r"^/records/(\d+)/([A-Za-z0-9_.-]+)$", p)
            if m:
                f = (SETTINGS.records_dir / m.group(1) / m.group(2)).resolve()
                if not str(f).startswith(str(SETTINGS.records_dir.resolve())) or not f.exists():
                    return self._json({"error": "not found"}, 404)
                ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                if f.suffix in (".md", ".json", ".txt"):
                    ctype = "text/plain; charset=utf-8"
                return self._send(200, f.read_bytes(), ctype)
            return self._json({"error": "not found"}, 404)
        except Exception as e:  # pragma: no cover
            log.exception("dashboard error")
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = {}
        if n:
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                body = {}
        m = re.match(r"^/api/job/(\d+)/(requeue|hold|dead|release|unrelease)$", u.path)
        if m:
            return self._json(action(int(m.group(1)), m.group(2), body))
        m = re.match(r"^/api/question/(\d+)/(answer|hold)$", u.path)
        if m:
            return self._json(answer_question(int(m.group(1)), m.group(2), str(body.get("text") or "")))
        if u.path == "/api/settings":
            return self._json(update_settings(body))
        if u.path == "/api/telegram/detect":
            return self._json(telegram_detect())
        if u.path == "/api/telegram/test":
            return self._json(telegram_test())
        if u.path == "/api/answer":
            return self._json(edit_answer(body))
        return self._json({"error": "not found"}, 404)


def serve(port: int = 8787, host: str | None = None) -> ThreadingHTTPServer:
    host = host or SETTINGS.dashboard_host
    db.connect()
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    log.info("dashboard on http://%s:%s", host, port)
    return srv


def start_in_thread(port: int = 8787) -> threading.Thread | None:
    try:
        srv = serve(port)
    except OSError as e:
        log.warning("dashboard not started: %s", e)
        return None
    t = threading.Thread(target=srv.serve_forever, name="dashboard", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    serve().serve_forever()
