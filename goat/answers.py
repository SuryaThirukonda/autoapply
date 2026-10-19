"""Versioned answer store keyed by normalized question keys.

Lookup order (answers.lookup):
  1. exact key                       (the model or an adapter names a key)
  2. alias: normalized question text (exact)       -> key
  3. alias: Hermes-style substring sets            -> key
  4. (caller may then try the gpt-6-luna mapper above a confidence threshold)
  5. miss -> ask_user

Write-back (save_answer) is ONE transaction: upsert the answer, supersede the old row, and
re-queue every job held on that key. The bot calls this; so does /remember after confirmation.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable

from . import db, states

_NORM = re.compile(r"[^a-z0-9]+")


def norm(text: str) -> str:
    return _NORM.sub(" ", (text or "").lower()).strip()


def key_from_question(text: str) -> str:
    """A stable, human-readable key for a brand-new question: q.<first 8 words>."""
    words = norm(text).split()
    return "q." + "_".join(words[:8]) if words else "q.unknown"


@dataclass
class Answer:
    key: str
    answer: str | None
    answer_type: str | None
    scope: str
    scope_value: str
    source: str
    note: str | None = None
    confidence: float = 1.0
    via: str = "key"  # key | alias_exact | alias_substrings | classifier

    @property
    def has_value(self) -> bool:
        if self.answer_type == "blank":
            return self.answer is not None      # "leave it empty" is a decision, not a gap
        return self.answer is not None and str(self.answer).strip() != ""

    @property
    def tier(self) -> str:
        return answer_tier(self)

    @property
    def is_blank(self) -> bool:
        return self.answer_type == "blank"

    @property
    def is_conditional(self) -> bool:
        return (self.answer_type or "").startswith("conditional:")


def _row_to_answer(row, via: str, confidence: float = 1.0) -> Answer:
    return Answer(key=row["key"], answer=row["answer"], answer_type=row["answer_type"],
                  scope=row["scope"], scope_value=row["scope_value"], source=row["source"],
                  note=row["note"], confidence=confidence, via=via)


# A dollar figure with a pay word nearby: "$20/hr", "$85,000 - $110,000 per year", "Pay range: $25 to $30".
_PAY_AMOUNT = re.compile(r"\$\s?\d{1,3}(?:,\d{3})*(?:\.\d+)?\s?[kK]?")
_PAY_WORDS = re.compile(r"(hour|hourly|/\s?hr|\bhr\b|per year|/\s?yr|annual|salary|pay|compensation|wage|stipend|base|range|USD|week)", re.I)


def posting_pay(text: str) -> str | None:
    """The first pay figure in a posting ("$20/hr", "$85,000 - $110,000 a year"), or None."""
    for m in _PAY_AMOUNT.finditer(text or ""):
        lo, hi = max(0, m.start() - 120), min(len(text), m.end() + 60)
        window = text[lo:hi]
        if _PAY_WORDS.search(window) and not re.search(r"(raised|funding|series [a-e]|valuation|revenue|million|billion|\d[mb]\b)", text[m.start():m.end() + 25], re.I):
            return " ".join(text[max(0, m.start() - 40):m.end() + 30].split())
    return None


def concrete_value(a: "Answer", *, company: str = "", posting_text: str = "", listing_id: str | None = None) -> tuple[str | None, str]:
    """The value to type for a stored answer, after resolving its type. Returns (value, why).
    blank -> ""; conditional:<cond> stores "<when true> / <when false>"; everything else is as stored."""
    if a.is_blank:
        return "", "leave this field empty"
    if not a.is_conditional:
        return (None if a.answer is None else str(a.answer)), ""
    cond = (a.answer_type or "").split(":", 1)[1]
    parts = [x.strip() for x in str(a.answer or "").split(" / ")]
    when_true = parts[0] if parts else ""
    when_false = parts[1] if len(parts) > 1 else when_true
    if cond == "today":
        from datetime import date
        return date.today().strftime("%m/%d/%Y"), "today's date"
    if cond == "signature_today":
        # one box for the e-signature AND the date ("Full name and today's date", Lyft 2026-10-18): his legal name + today
        from datetime import date
        name = get("identity.legal_name")
        legal = str(name.answer).strip() if name is not None and name.answer else ""
        if not legal:
            return None, "no stored legal name"
        return f"{legal} {date.today().strftime('%m/%d/%Y')}", "his legal name and today's date"
    if cond == "posting_lists_pay":
        hit = posting_pay(posting_text)
        return (when_true, f"the posting lists pay: {hit[:60]}") if hit else (when_false, "the posting lists no pay")
    if cond == "ledger_company":
        from . import ledger
        n = ledger.company_application_count(company, exclude_listing=listing_id)
        return (when_true, f"{n} earlier application(s) to {company} in the ledger") if n else (when_false, f"no earlier application to {company} in the ledger")
    return None, f"unknown condition {cond}"


def get(key: str, company: str = "", ats: str = "") -> Answer | None:
    """Most specific live row for a key: company > ats > global."""
    con = db.connect()
    candidates = []
    if company:
        candidates.append(("company", norm(company)))
    if ats:
        candidates.append(("ats", ats.lower()))
    candidates.append(("global", ""))
    for scope, sv in candidates:
        row = con.execute(
            "SELECT * FROM answers WHERE key=? AND scope=? AND scope_value=? AND superseded_by IS NULL "
            "AND approved=1 ORDER BY id DESC LIMIT 1", (key, scope, sv)).fetchone()
        if row:
            return _row_to_answer(row, "key")
    return None


def key_exists(key: str) -> bool:
    return db.connect().execute(
        "SELECT 1 FROM answers WHERE key=? AND superseded_by IS NULL LIMIT 1", (key,)).fetchone() is not None


def all_keys() -> list[str]:
    return [r["key"] for r in db.connect().execute(
        "SELECT DISTINCT key FROM answers WHERE superseded_by IS NULL ORDER BY key")]


def resolve_alias(question: str) -> tuple[str, float, str] | None:
    """Map question text to a key via aliases. Returns (key, confidence, via) or None."""
    q = norm(question)
    if not q:
        return None
    con = db.connect()
    # Newest wins a tie: an alias Surya's answer created beats one imported from the old pipeline.
    row = con.execute(
        "SELECT key, confidence FROM question_aliases WHERE kind='exact' AND alias=? AND approved=1 "
        "ORDER BY confidence DESC, id DESC LIMIT 1", (q,)).fetchone()
    if row:
        return row["key"], float(row["confidence"]), "alias_exact"
    best = None
    for r in con.execute("SELECT alias, key, confidence FROM question_aliases WHERE kind='substrings' AND approved=1"):
        try:
            sets = json.loads(r["alias"])
        except json.JSONDecodeError:
            continue
        for needles in sets:
            if all(norm(n) in q for n in needles):
                score = float(r["confidence"]) * (1 + sum(len(n) for n in needles) / 1000.0)
                if best is None or score > best[1]:
                    best = (r["key"], score)
    if best:
        return best[0], min(1.0, best[1]), "alias_substrings"
    return None


def lookup(question: str, company: str = "", ats: str = "") -> Answer | None:
    """Deterministic lookup only (no model). None means: try the classifier, then ask."""
    hit = resolve_alias(question)
    if not hit:
        return None
    key, conf, via = hit
    a = get(key, company, ats)
    if a is None:
        return None
    a.confidence = conf
    a.via = via
    return a


# Confidence tiers (roadmap 2026-10-18 item 2). confirmed: Surya said so (questionnaire, Telegram, the web form,
# an operator command, the Hermes-era bank he reviewed). inferred: the agent derived or defaulted it. sensitive:
# self-identification, consent and policy answers, which are re-shown whatever their source.
CONFIRMED_SOURCES = ("questionnaire", "telegram", "web", "operator", "remember", "hermes", "surya", "seed:", "nightly:approved", "user:")
INFERRED_SOURCES = ("derived", "default_no", "only_option", "profile", "classifier", "seed:derived", "generated", "judgment")
SENSITIVE_KEY_PREFIXES = ("self_id.", "consent.", "policy.", "compliance.", "compensation.", "identity.dob", "identity.date_of_birth", "identity.ssn")


def tier_for(key: str | None, source: str | None) -> str:
    k = (key or "").lower()
    if any(k.startswith(p) for p in SENSITIVE_KEY_PREFIXES):
        return "sensitive"
    s = (source or "").lower()
    if any(s.startswith(p) for p in INFERRED_SOURCES):
        return "inferred"
    if any(s.startswith(p) for p in CONFIRMED_SOURCES):
        return "confirmed"
    return "inferred"


def answer_tier(a: "Answer") -> str:
    return tier_for(a.key, a.source)


def decision_tier(source: str | None, field_label: str = "", company: str = "", ats: str = "") -> str:
    """The tier of a recorded decision from its source tag: standing:<key> looks the answer up; resume,
    adapter and as-is values are confirmed; generated / judgment / unknown are inferred."""
    s = (source or "").strip()
    low = s.lower()
    if low.startswith(("standing:", "bank:", "policy:", "approved_wording:")):
        key = s.split(":", 1)[1].split(" ", 1)[0].strip()
        if low.startswith("policy:"):
            return "sensitive"
        if low.startswith("bank:"):
            return "confirmed"            # the answer bank is Surya's own reviewed wording (Hermes era)
        a = get(key, company, ats)
        return answer_tier(a) if a else tier_for(key, "")
    if low.startswith(("resume", "adapter", "cleared", "as_is", "user:", "seed:", "standing")):
        return "confirmed"
    return "inferred"


def unconfirmed_source(source: str | None, company: str = "", ats: str = "") -> tuple[bool, str]:
    """Whether a recorded decision's value still needs checking before an automatic submit, and the evidence
    the agent had for it. Unconfirmed: text the agent wrote (generated), its own judgment, a value kept as the
    page showed it (user:manual), and a standing answer whose stored source is derived, default_no, profile or
    classifier (the stored note says how it was derived). Confirmed: Surya's questionnaire, Telegram and web
    answers, the reviewed answer bank, the résumé, adapter fills from standing answers, account policy."""
    s = (source or "").strip()
    low = s.lower()
    if low.startswith(("standing:", "approved_wording:")):
        key = s.split(":", 1)[1].split(" ", 1)[0].strip()
        a = get(key, company, ats)
        if a is None:
            return True, ""
        src = (a.source or "").lower()
        confirmed = any(src.startswith(p) for p in CONFIRMED_SOURCES) and not any(src.startswith(p) for p in INFERRED_SOURCES)
        return (False, "") if confirmed else (True, str(a.note or "")[:400])
    if low.startswith(("bank:", "policy:", "resume", "adapter", "cleared", "seed:", "operator", "user:surya", "user:telegram", "user:web")):
        return False, ""
    if low.startswith("user:manual"):
        return True, "kept as the page showed it"
    return True, ""


def save_answer(key: str, answer: str | None, *, answer_type: str = "text", source: str,
                source_ref: str = "", scope: str = "global", scope_value: str = "",
                note: str | None = None, approved: bool = True,
                aliases: Iterable[str] = ()) -> int:
    """Upsert an answer and release every job held on this key, in ONE transaction.
    Returns the number of jobs released."""
    scope_value = norm(scope_value) if scope == "company" else (scope_value or "").lower()
    with db.tx() as con:
        old = con.execute(
            "SELECT id FROM answers WHERE key=? AND scope=? AND scope_value=? AND superseded_by IS NULL",
            (key, scope, scope_value)).fetchall()
        cur = con.execute(
            "INSERT INTO answers(key,scope,scope_value,answer,answer_type,note,source,source_ref,approved,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (key, scope, scope_value, answer, answer_type, note, source, source_ref, int(approved), db.now()))
        new_id = cur.lastrowid
        for r in old:
            con.execute("UPDATE answers SET superseded_by=? WHERE id=?", (new_id, r["id"]))
        for al in aliases:
            add_alias(al, key, source=source, con=con)
        released = 0
        if approved and answer is not None:
            cur = con.execute(
                "UPDATE jobs SET status=?, blocked_on_question_key=NULL, hold_reason=NULL, "
                "priority=priority+10, updated_at=? WHERE status=? AND blocked_on_question_key=?",
                (states.QUEUED, db.now(), states.HOLD, key))
            released = cur.rowcount
            # Also wake jobs that are mid-wait on this key (awaiting_answer): they poll the store.
        con.execute("INSERT INTO events(job_id,ts,kind,detail) VALUES(NULL,?,?,?)",
                    (db.now(), "answer:saved", json.dumps({"key": key, "source": source, "released": released})))
    return released


def add_alias(alias: str, key: str, *, kind: str = "exact", confidence: float = 1.0,
              source: str = "", approved: bool = True, con=None) -> None:
    a = norm(alias) if kind == "exact" else alias
    if not a:
        return
    sql = ("INSERT INTO question_aliases(alias,kind,key,confidence,approved,source,created_at) "
           "SELECT ?,?,?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM question_aliases WHERE alias=? AND key=?)")
    args = (a, kind, key, confidence, int(approved), source, db.now(), a, key)
    if con is not None:
        con.execute(sql, args)
    else:
        with db.tx() as c:
            c.execute(sql, args)


def record_classifier_alias(question: str, key: str, confidence: float) -> None:
    """A model-made mapping is recorded unapproved so the nightly job can propose it."""
    add_alias(question, key, confidence=confidence, source="classifier", approved=False)


# --------------------------------------------------------------------------- seeding

def _flatten(prefix: str, obj) -> Iterable[tuple[str, object]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.startswith("_"):
                continue
            yield from _flatten(f"{prefix}.{k}" if prefix else k, v)
    else:
        yield prefix, obj


def seed_from_config(config_dir) -> dict:
    """Seed the store from the Hermes-era files copied into config/. Idempotent: existing
    live rows for a key are left alone unless the value changed."""
    from pathlib import Path
    import yaml  # type: ignore

    cfg = Path(config_dir)
    counts = {"answers": 0, "aliases": 0}

    def put(key, value, atype, source, note=None, aliases=()):
        if isinstance(value, list):
            value = "; ".join(str(x) for x in value)
        cur = get(key)
        if cur is not None and cur.answer == (None if value is None else str(value)) and cur.source == source:
            for al in aliases:
                add_alias(al, key, source=source)
            return
        save_answer(key, None if value is None else str(value), answer_type=atype, source=source,
                    note=note, aliases=aliases)
        counts["answers"] += 1

    sa = json.loads((cfg / "standing_answers.json").read_text(encoding="utf-8"))
    for key, value in _flatten("", sa):
        last = key.rsplit(".", 1)[-1]
        if last in ("note", "rule", "rules", "comment") or key.endswith(("_note", "_rule", "_rules", "_variants")):
            continue
        if isinstance(value, dict):
            continue
        put(key, value, "text", "seed:standing_answers.json")

    bank = json.loads((cfg / "answer_bank.json").read_text(encoding="utf-8"))
    for e in bank.get("entries", []):
        key = f"bank.{e['id']}"
        scope, sv = "global", ""
        if e.get("company"):
            scope, sv = "company", e["company"]
        cur = get(key, company=sv if scope == "company" else "")
        if cur is None or cur.answer != (None if e.get("answer") is None else str(e.get("answer"))):
            save_answer(key, e.get("answer"), answer_type=e.get("type", "text"), source="seed:answer_bank.json",
                        note=e.get("note"), scope=scope, scope_value=sv)
            counts["answers"] += 1
        add_alias(json.dumps(e.get("match", [])), key, kind="substrings", source="seed:answer_bank.json")
        counts["aliases"] += 1
    for prompt_kind, story in (bank.get("story_map") or {}).items():
        if prompt_kind.startswith("_"):
            continue
        put(f"story.{norm(prompt_kind).replace(' ', '_')[:60]}", story, "story", "seed:answer_bank.json")

    deleg = yaml.safe_load((cfg / "delegation.yaml").read_text(encoding="utf-8")) or {}
    for k, v in (deleg.get("self_identification") or {}).items():
        put(f"self_id.{k}", v, "choice", "seed:delegation.yaml")
    for k, v in (deleg.get("authority") or {}).items():
        put(f"policy.{k}", "Yes" if v is True else ("No" if v is False else v), "policy", "seed:delegation.yaml")
    aiu = deleg.get("ai_use_disclosure") or {}
    put("policy.ai_use_disclosure.option", aiu.get("preferred_option_text"), "choice", "seed:delegation.yaml")
    put("policy.ai_use_disclosure.text",
        "An AI agent filled this form using information I supplied and approved.", "text", "seed:delegation.yaml")
    put("policy.ai_use_disclosure.never_answer_no", "Yes", "policy", "seed:delegation.yaml")
    for item in deleg.get("always_hold") or []:
        put(f"always_hold.{item}", None, "policy", "seed:delegation.yaml")

    # Attestations and consents the policy allows, as keys the model can name directly.
    put("consent.truthfulness_attestation", "Yes", "choice", "seed:delegation.yaml",
        note="Tick the final 'I certify the information is true and complete' box only when the form is complete (delegation: may_tick_truthfulness_attestation).",
        aliases=["i certify that the information provided is true and complete",
                 "i certify that the information provided is true and complete *",
                 "i certify that all information provided is true and accurate",
                 "certify true and complete", "i attest that the information is accurate"])
    put("consent.privacy_notice", "Yes", "choice", "seed:delegation.yaml",
        note="Privacy notice / data-processing consent required to apply: accept (delegation).",
        aliases=["i have read and agree to the privacy notice", "i acknowledge the privacy policy", "i agree to the terms and conditions"])
    put("consent.sms", "Yes", "choice", "seed:delegation.yaml", note="SMS / text notifications: always opt in.")
    put("consent.marketing_email", "No", "choice", "seed:delegation.yaml", note="Marketing email and talent community: never opt in.")
    # The spec's four defaults, stated once more with tight scope.
    put("defaults.us_citizen", "Yes", "choice", "seed:spec",
        aliases=["are you a u s citizen", "us citizen"])
    put("defaults.authorized_to_work", "Yes", "choice", "seed:spec")
    put("defaults.needs_sponsorship", "No", "choice", "seed:spec")
    put("defaults.not_military", "No", "choice", "seed:spec",
        note="Veteran / military service: No. Does NOT settle a military-spouse question.")
    return counts
