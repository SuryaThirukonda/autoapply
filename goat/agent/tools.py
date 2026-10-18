"""Function tools the model can call, and the dispatcher that runs them. Every consequential action
lives here as deterministic code; the model only requests it. Secrets never pass through."""
from __future__ import annotations

import json
import re
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import answers, db, profile, records, states
from ..budget import Budget, Parked
from ..browser import fill, snapshot as snapmod
from ..browser.captcha import detect_challenge
from ..browser.executor import ClickRefused, Executor, NavigationRefused
from ..browser.submit import submit as do_submit
from ..config import SETTINGS
from ..gate import guardrail_check, run_gate
from ..human.questions import BROKER

log = logging.getLogger(__name__)

FUNCTION_TOOLS: list[dict] = [
    {"type": "function", "name": "snapshot", "strict": True,
     "description": "Read the page as a pruned accessibility outline with refs [eN], headed by the error summary. Call it on every new page and once after filling a page.",
     "parameters": {"type": "object", "properties": {"with_text": {"type": "boolean", "description": "also include the first 6000 characters of visible page text"}},
                    "required": ["with_text"], "additionalProperties": False}},
    {"type": "function", "name": "fill_batch", "strict": True,
     "description": "Set many plain fields at once (text, textarea, email, tel, number, native select, radio, checkbox). Each field names a ref and EITHER an answer_key from the ANSWERS list (preferred) OR a literal value with a source. Returns per-field set/failed with the value read back.",
     "parameters": {"type": "object", "properties": {"fields": {"type": "array", "items": {"type": "object", "properties": {
         "ref": {"type": "string"},
         "answer_key": {"type": ["string", "null"], "description": "key from ANSWERS or from lookup_answer"},
         "value": {"type": ["string", "null"], "description": "literal value when no key applies (true/false for checkboxes and radios)"},
         "source": {"type": ["string", "null"], "description": "required with a literal: 'generated' for free-response text you wrote under the writing rules, 'resume' for a value copied verbatim from the résumé text, 'clear' with an empty value to blank a field holding a stale or wrong value, 'as_is' (value null) to keep what the field already shows because Surya set it himself in the browser or told you it is done"},
         "label": {"type": "string", "description": "the field label as shown"}},
         "required": ["ref", "answer_key", "value", "source", "label"], "additionalProperties": False}}},
                    "required": ["fields"], "additionalProperties": False}},
    {"type": "function", "name": "choose", "strict": True,
     "description": "Pick an option in a custom dropdown, combobox or autocomplete (react-select, Workday 'Select One', location pickers). Give the option texts to try in order (an answer_key may supply them). Verified by read-back.",
     "parameters": {"type": "object", "properties": {"ref": {"type": "string"}, "answer_key": {"type": ["string", "null"]},
                                                     "options": {"type": "array", "items": {"type": "string"}, "description": "option texts to try, most specific first"},
                                                     "label": {"type": "string"}, "source": {"type": ["string", "null"]}},
                    "required": ["ref", "answer_key", "options", "label", "source"], "additionalProperties": False}},
    {"type": "function", "name": "click", "strict": True,
     "description": "Click an element by ref (Next, Save and Continue, Add, open a section). Submit controls are refused: use submit.",
     "parameters": {"type": "object", "properties": {"ref": {"type": "string"}, "label": {"type": "string"}},
                    "required": ["ref", "label"], "additionalProperties": False}},
    {"type": "function", "name": "upload_resume", "strict": True,
     "description": "Attach Surya's résumé (or transcript) to a file input by ref. Only files from the résumé folder. Verifies the filename on the page.",
     "parameters": {"type": "object", "properties": {"ref": {"type": "string"}, "document": {"type": "string", "enum": ["resume", "transcript"]}},
                    "required": ["ref", "document"], "additionalProperties": False}},
    {"type": "function", "name": "set_date", "strict": True,
     "description": "Fill a date field (graduation date, employment dates). Handles Workday's three-section month/day/year widget, which ignores ordinary typing. Give the date as MM/DD/YYYY, MM/YYYY or 'May 2028'; an answer_key may supply it.",
     "parameters": {"type": "object", "properties": {"ref": {"type": "string"}, "answer_key": {"type": ["string", "null"]},
                                                     "value": {"type": ["string", "null"]}, "label": {"type": "string"}},
                    "required": ["ref", "answer_key", "value", "label"], "additionalProperties": False}},
    {"type": "function", "name": "zoom", "strict": True,
     "description": "Return a full-resolution crop of the current viewport (x, y, width, height in screenshot pixels) to read small text.",
     "parameters": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "width": {"type": "integer"}, "height": {"type": "integer"}},
                    "required": ["x", "y", "width", "height"], "additionalProperties": False}},
    {"type": "function", "name": "lookup_answer", "strict": True,
     "description": "Find the answer to a form question in Surya's knowledge base before ever asking him. Give the question as labelled, every answer choice you can see ([] if none are visible), your plain reading of what it asks, and the field's ref: the harness screenshots the field, opens a dropdown to read all its choices (then closes it), and a resolver matches the meaning (not just the wording) against his stored answers and profile. Returns {key, answer, via} to fill with that key, or a miss.",
     "parameters": {"type": "object", "properties": {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}},
                                                     "meaning": {"type": ["string", "null"], "description": "what the question asks, in your own words, from the page and the screenshot"},
                                                     "ref": {"type": ["string", "null"], "description": "the field's ref, so the harness can see it and read its choices"}},
                    "required": ["question", "options", "meaning", "ref"], "additionalProperties": False}},
    {"type": "function", "name": "ask_user", "strict": True,
     "description": "Send the page's unknown REQUIRED questions to Surya's phone right away (one message) and return immediately. Keep filling the rest of the page. Returns the keys to pass to await_answers.",
     "parameters": {"type": "object", "properties": {"questions": {"type": "array", "items": {"type": "object", "properties": {
         "question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}, "field_type": {"type": "string"}, "ref": {"type": "string"}},
         "required": ["question", "options", "field_type", "ref"], "additionalProperties": False}}},
                    "required": ["questions"], "additionalProperties": False}},
    {"type": "function", "name": "await_answers", "strict": True,
     "description": "Wait for Surya's answers to keys from ask_user. The 15-minute clock started at ask time. Call this only when everything else on the page is done. If time runs out the run is parked automatically.",
     "parameters": {"type": "object", "properties": {"keys": {"type": "array", "items": {"type": "string"}}},
                    "required": ["keys"], "additionalProperties": False}},
    {"type": "function", "name": "guardcheck", "strict": True,
     "description": "Check text you wrote against the accuracy guardrails before typing it. BLOCK means rewrite or park.",
     "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}},
    {"type": "function", "name": "credentials", "strict": True,
     "description": "Fill the email and password boxes of a sign-in, account-creation or password-reset form on this portal through the password helper. Modes: 'login_universal' = Surya's one application password (try FIRST on a Sign In page); 'login' = the password stored for this exact host; 'create' = a new account with that same password; 'set_password' = on a password-RESET form, type the new password (and its confirm box) only; email_ref may be '' then. Returns 'filled', 'filled_universal', 'filled_fallback' or 'exists_unknown'. You never see the password.",
     "parameters": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["create", "login", "login_universal", "set_password"]}, "email_ref": {"type": "string"},
                                                     "password_ref": {"type": "string"}, "confirm_password_ref": {"type": ["string", "null"]}},
                    "required": ["mode", "email_ref", "password_ref", "confirm_password_ref"], "additionalProperties": False}},
    {"type": "function", "name": "open_password_reset_link", "strict": True,
     "description": "After you asked the portal to reset the password (Forgot your password -> email -> send), wait for its reset email and open the reset link. Then call credentials with mode 'set_password' on the new-password boxes, click the save button, and sign in with login_universal. Returns 'opened', 'not_received' or 'host_mismatch'.",
     "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    {"type": "function", "name": "enter_email_code", "strict": True,
     "description": "Fetch the verification code this portal just emailed and type it into the code boxes (refs in order). Returns 'entered' or 'not_received'. The code never comes to you.",
     "parameters": {"type": "object", "properties": {"code_refs": {"type": "array", "items": {"type": "string"}}},
                    "required": ["code_refs"], "additionalProperties": False}},
    {"type": "function", "name": "open_verification_link", "strict": True,
     "description": "After account creation, wait for the portal's verification email and open its link in this tab if, and only if, the link's host matches this portal. Returns verified, host_mismatch or not_received.",
     "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    {"type": "function", "name": "submit", "strict": True,
     "description": "The only path to submission. Runs the code gate, refuses while the page shows errors or empty required fields, clicks once, and reports submitted / blocked / failed with reasons. Call it when every page is complete.",
     "parameters": {"type": "object", "properties": {"button_label": {"type": ["string", "null"], "description": "exact submit button text if several submit-like buttons exist"}},
                    "required": ["button_label"], "additionalProperties": False}},
    {"type": "function", "name": "go_to_step", "strict": True,
     "description": "Workday only: jump to a completed step of the application by its name in the progress rail at the top ('My Information', 'My Experience', 'Application Questions 1 of 2', 'Voluntary Disclosures', 'Review'). Use it from the Review page when a section shows 'No Response' that you must fill, then Save and Continue forward again.",
     "parameters": {"type": "object", "properties": {"step": {"type": "string"}}, "required": ["step"], "additionalProperties": False}},
    {"type": "function", "name": "tabs", "strict": True,
     "description": "This application's browser tabs. A click that opens a new tab on the portal moves you into it automatically; use this to see them or move between them. Actions: 'list'; 'switch' (index); 'close' (index, never the last tab); 'back' (browser Back in this tab); 'reload'; 'goto' (url, only hosts of this application or its portal).",
     "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "switch", "close", "back", "reload", "goto"]},
                                                     "index": {"type": ["integer", "null"]}, "url": {"type": ["string", "null"]}},
                    "required": ["action", "index", "url"], "additionalProperties": False}},
    {"type": "function", "name": "park", "strict": True,
     "description": "End the run with a named reason. Use 'ready_for_you: <what is left>' when only a human-verification challenge and Submit remain; otherwise a hold reason such as 'no_standing_answer: <label>', 'bot_check_question: <text>', 'automation_prohibited: <text>', 'eligibility: <why>'.",
     "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"], "additionalProperties": False}},
]


@dataclass
class RunContext:
    job: dict                      # row from jobs joined with listing fields
    ex: Executor
    budget: Budget
    page_no: int = 1
    free_text: list[dict] = field(default_factory=list)
    asked_keys: list[str] = field(default_factory=list)
    last_snapshot: snapmod.Snapshot | None = None
    consecutive_failures: int = 0
    resume_file: str | None = None
    submitted: dict | None = None
    gate_result: dict | None = None
    classifier: Any = None         # agent.classifier.Classifier
    mail: Any = None               # goat.mail.gmail module-level functions
    creds: Any = None              # goat.creds.bitwarden
    code_requested_at: int = 0
    park_attempts: int = 0
    posting_text: str = ""         # the posting page's text at open (pay figures, requirements)
    verify_link_tried: bool = False
    consent_page_key: str = ""
    resolved: dict = field(default_factory=dict)   # norm(question) -> resolver outcome this run
    control_failures: dict = field(default_factory=dict)   # ref -> failed choose / set_date calls this run

    @property
    def job_id(self) -> int:
        return int(self.job["id"])

    @property
    def portal(self) -> str:
        return self.job.get("ats") or "other"

    @property
    def portal_host(self) -> str:
        from urllib.parse import urlparse
        return (urlparse(self.ex.url or self.job.get("url", "")).hostname or "").lower()


def _resume_path(ctx: RunContext, document: str) -> Path | None:
    import yaml
    deleg = yaml.safe_load((SETTINGS.config_dir / "delegation.yaml").read_text(encoding="utf-8")) or {}
    res = deleg.get("resumes") or {}
    if document == "transcript":
        tf = answers.get("education.transcript_file")
        if tf and tf.answer:
            p = SETTINGS.resume_dir / Path(tf.answer).name
            return p if p.exists() else SETTINGS.resume_dir / "transcripts" / Path(tf.answer).name
        return None
    from ..adapters import resume_path_for
    return resume_path_for(ctx.job)


_RESUME_TEXT: str | None = None


def _resume_text() -> str:
    global _RESUME_TEXT
    if _RESUME_TEXT is None:
        parts = []
        for p in SETTINGS.resume_dir.glob("*.txt"):
            try:
                parts.append(p.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass
        _RESUME_TEXT = answers.norm(" ".join(parts))
    return _RESUME_TEXT


def _in_resume(value: str) -> bool:
    """Every comma/semicolon-separated token of the value must appear in the résumé text."""
    toks = [answers.norm(t) for t in re.split(r"[,;/\n]+", str(value)) if answers.norm(t)]
    text = _resume_text()
    return bool(toks) and all(t in text for t in toks)


NEGATED = re.compile(r"^(not |no |none|i do not|i am not|unwilling|unable|never|decline|prefer not)", re.I)
AFFIRMATIVE = {"yes", "true", "checked", "on", "y"}


# Yes/no questions about where Surya works or worked, industries, affiliations and relationships.
# Surya, 2026-10-15: "answer is almost always no (that's default if it stalls)".
AFFILIATION = re.compile(
    r"(employed (by|with|at)\b|\bwork(ed|ing)? (for|at)\b|\baffiliat|relationships? (with|to)\b|\brelated to\b|\brelatives?\b|family member|"
    r"household member|immediate family|\bspouse\b|board member|\bofficers?\b|\bdirectors?\b|government official|public official|"
    r"conflict of interest|financial interest|\bown (stock|shares)|\bvendors?\b|\bsuppliers?\b|\bcustomers?\b|\bclients?\b|"
    r"\bcompetitors?\b|subsidiar|parent company|former employee|current employee|independent contractor|consultant (for|to)|"
    r"\bindustry\b)", re.I)
_YES = re.compile(r"^(yes|y|true)\b", re.I)
SELECT_ALL = {"all", "all offered locations", "all offered", "all locations", "all of the above", "select all", "every location", "any"}
_NO = re.compile(r"^(no|n|false|none)\b", re.I)
FREE_RESPONSE = re.compile(
    r"^(why\b|describe|tell (us|me)|explain|share\b|what (interests|excites|motivates|makes|draws|attracts)|how (would|did|have|do) you|"
    r"please (describe|explain|tell|share|provide a (brief|short))|give (us )?an example|walk (us|me) through|what is (a|one|the) "
    r"(project|accomplishment|achievement|challenge)|what are you (most )?(proud|passionate|looking)|in (\d+|one|two|three) "
    r"(words|sentences)|cover letter|anything else)", re.I)


SOCIAL_SIGNIN = re.compile(r"(sign in|sign up|log ?in|continue|apply|register)\s+(with|using|via)\s+(google|linkedin|apple|microsoft|facebook|indeed)", re.I)


def is_affiliation_question(question: str, options: list[str] | None) -> bool:
    """A yes/no question in the employer/industry/affiliation family (options empty, or Yes/No-like)."""
    if not AFFILIATION.search(question or ""):
        return False
    if re.search(r"(if (yes|so)|please (explain|describe|list|provide)|who\b|which\b|name of|names of)", question, re.I) and not options:
        return False
    opts = [o.strip() for o in (options or []) if o and o.strip() and not re.match(r"^(select|choose|please select)", o.strip(), re.I)]
    if not opts:
        return True
    return len(opts) <= 4 and any(_YES.match(o) for o in opts) and any(_NO.match(o) for o in opts)


def consent_question_key(question: str) -> str | None:
    """Consent / acknowledgement / certification questions Surya answers yes to on every form (his rule,
    2026-10-15), mapped to the standing key that authorizes it. Marketing opt-ins map to the No key."""
    from ..adapters import CONSENT, MARKETING, PROCESSING, SMS, consent_key
    q = question or ""
    asks_consent = re.search(r"\b(do|will|would) you (agree|accept|acknowledge|consent|certify|authori[sz]e|understand|confirm)\b", q, re.I)
    if SMS.search(q) and (asks_consent or CONSENT.search(q) or re.search(r"\b(receive|opt in|opt-in)\b", q, re.I)):
        return "consent.sms"
    if not CONSENT.search(q) and not MARKETING.search(q) and not asks_consent:
        return None
    if MARKETING.search(q) and not SMS.search(q) and not PROCESSING.search(q):
        return "consent.marketing_email"
    return consent_key(q)


def is_free_response(question: str, options: list[str] | None, field_type: str = "") -> bool:
    if options:
        return False
    if (field_type or "").lower() in ("textarea", "richtext"):
        return True
    return bool(FREE_RESPONSE.search((question or "").strip()))


# Questions the resolver may match to a STORED answer but never answer from the profile on its own.
NO_DERIVE = re.compile(
    r"(social security|\bssn\b|passport|driver'?s licen[cs]e|government[- ]issued|national id|date of birth|\bdob\b|birth ?date|"
    r"criminal|convict|felony|misdemeanou?r|arrest|disciplin|reference|salary|compensation expectation|desired (pay|salary)|"
    r"pay expectation|gender|\brace\b|ethnic|hispanic|latin[oax]|veteran|disabilit|sexual orientation|transgender|pronoun)", re.I)


def _answer_rows_for_prompt(company: str, ats: str) -> list[tuple[str, str, str]]:
    """Keys shown in the cached prefix: standing identity/education/work-auth/availability/background,
    self-ID and bank entries. Policy flags and stories are omitted (looked up on demand)."""
    con = db.connect()
    rows = []
    for r in con.execute("SELECT key, answer, answer_type, note FROM answers WHERE superseded_by IS NULL AND approved=1 AND answer IS NOT NULL "
                         "AND scope='global' AND (key LIKE 'identity.%' OR key LIKE 'education.%' OR key LIKE 'work_authorization.%' "
                         "OR key LIKE 'availability.%' OR key LIKE 'background.%' OR key LIKE 'self_id.%' OR key LIKE 'bank.%' "
                         "OR key LIKE 'defaults.%' OR key LIKE 'experience_pickers.%' OR key LIKE 'how_did_you_hear.%' "
                         "OR key LIKE 'policy.ai_use_disclosure.%' OR key LIKE 'policy.may_%' "
                         "OR key LIKE 'consent.%' OR key LIKE 'compliance.%' OR key LIKE 'relationships.%' OR key LIKE 'experience.%' "
                         "OR key LIKE 'preferences.%' OR key LIKE 'compensation.%') ORDER BY key"):
        if r["key"].endswith(".note") or r["key"].endswith("_note") or r["key"].endswith("_rule") or r["key"].endswith("_rules"):
            continue
        a = str(r["answer"])
        atype = r["answer_type"] or ""
        if atype == "blank":
            a = "(leave empty)"
        elif atype.startswith("conditional:"):
            a = f"{a}  [{atype}: pass the key; the harness picks the value]"
        if len(a) > 160:
            a = a[:157] + "..."
        rows.append((r["key"], a, r["note"] or ""))
    # company-scoped overrides
    for r in con.execute("SELECT key, answer, note FROM answers WHERE superseded_by IS NULL AND approved=1 AND answer IS NOT NULL "
                         "AND ((scope='company' AND scope_value=?) OR (scope='ats' AND scope_value=?))", (answers.norm(company), ats)):
        rows.append((r["key"], str(r["answer"])[:160], f"[{company}] " + (r["note"] or "")))
    return rows


class ToolDispatcher:
    def __init__(self, ctx: RunContext):
        self.ctx = ctx

    async def dispatch(self, name: str, arguments: str) -> tuple[str, bytes | None]:
        """Returns (json text for the model, optional image bytes to attach)."""
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return json.dumps({"error": "arguments were not valid JSON"}), None
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return json.dumps({"error": f"unknown tool {name}"}), None
        t0 = time.monotonic()
        try:
            out, img = await fn(**args)
            ok = not (isinstance(out, dict) and out.get("error"))
            self.ctx.consecutive_failures = 0 if ok else self.ctx.consecutive_failures + 1
            db.event(self.ctx.job_id, f"tool:{name}", {"args": args, "out": out}, latency_ms=int((time.monotonic() - t0) * 1000))
            return json.dumps(out, default=str), img
        except (Parked,):
            raise
        except (ClickRefused, NavigationRefused) as e:
            self.ctx.consecutive_failures += 1
            db.event(self.ctx.job_id, f"tool:{name}:refused", {"args": args, "error": str(e)})
            return json.dumps({"error": str(e)}), None
        except Exception as e:  # tool bugs must not kill the run silently
            self.ctx.consecutive_failures += 1
            log.exception("tool %s failed", name)
            db.event(self.ctx.job_id, f"tool:{name}:error", {"args": args, "error": str(e)[:400]})
            return json.dumps({"error": f"{type(e).__name__}: {str(e)[:300]}"}), None

    # ------------------------------------------------------------------ tools
    async def t_snapshot(self, with_text: bool = False):
        import asyncio
        snap = await snapmod.take(self.ctx.ex, with_text=with_text)
        # Workday renders its forms late: an apply page with almost nothing on it is usually still loading.
        for _ in range(3):
            if "/apply" not in (snap.url or "") or len([i for i in snap.items if i.get("tag") != "a"]) > 2:
                break
            await asyncio.sleep(2.5)
            snap = await snapmod.take(self.ctx.ex, with_text=with_text)
        # The consent pass runs on every new page, not only the first one the job opened on.
        page_key = (snap.url or "") + "|" + "|".join(snap.headings[:4])
        if page_key != self.ctx.consent_page_key:
            self.ctx.consent_page_key = page_key
            try:
                from ..adapters import PrefillResult, for_portal
                res = PrefillResult(details=[])
                await for_portal(self.ctx.portal)._consents(self.ctx.ex, snap, self.ctx.job, self.ctx.page_no, res)
                if res.set:
                    db.event(self.ctx.job_id, "adapter:consents", {"set": res.set, "details": res.details})
                    snap = await snapmod.take(self.ctx.ex, with_text=with_text)
            except Exception as e:
                db.event(self.ctx.job_id, "adapter:consents:error", {"error": str(e)[:200]})
        self.ctx.last_snapshot = snap
        text = snapmod.render(snap)
        if with_text and snap.text:
            text += "\n\nPAGE TEXT:\n" + snap.text[:6000]
        return {"outline": text}, None

    def _resolve_value(self, answer_key: str | None, value: str | None, source: str | None, label: str) -> tuple[str | None, str, str | None]:
        """-> (value, source_tag, error)"""
        if answer_key:
            a = answers.get(answer_key, self.ctx.job.get("company", ""), self.ctx.portal)
            if a is None:
                return None, "", f"unknown answer key {answer_key}"
            if not a.has_value:
                return None, "", f"{answer_key} has no standing answer: ask_user or park"
            val, why = answers.concrete_value(a, company=self.ctx.job.get("company", ""), posting_text=self.ctx.posting_text,
                                              listing_id=self.ctx.job.get("listing_id"))
            if val is None:
                return None, "", f"{answer_key}: {why}"
            tag = f"standing:{answer_key}" if not answer_key.startswith("bank.") else f"bank:{answer_key[5:]}"
            if why and not a.is_blank:
                tag += f" ({why[:80]})"
            return val, tag, None
        if value is None:
            return None, "", "neither answer_key nor value given"
        src = (source or "").lower()
        if src == "clear":
            if str(value).strip() not in ("", "false"):
                return None, "", "source 'clear' only takes an empty value (or false for a checkbox)"
            return str(value), "cleared", None
        if src not in ("generated", "resume"):
            return None, "", "a literal value needs source 'generated' (free text you wrote) or 'resume' (a value copied from the résumé); everything else needs an answer_key"
        if src == "resume":
            if not _in_resume(value):
                return None, "", f"'{str(value)[:60]}' does not appear in the résumé text; use an answer_key or ask_user"
        if src == "generated":
            # Generated text is for free-response boxes only. A choice (radio, checkbox, select,
            # dropdown, yes/no) must come from a stored answer key: call lookup_answer or ask_user.
            if str(value).strip().lower() in ("true", "false", "yes", "no", "checked", "unchecked", "on", "off", "n/a"):
                return None, "", "a choice or yes/no value cannot be 'generated'; use an answer_key (lookup_answer) or ask_user"
            if len(str(value).split()) < 6:
                return None, "", "'generated' is only for free-response text (6+ words); short facts need an answer_key, 'resume' or 'adapter'"
            blocks, _ = guardrail_check(value)
            if blocks:
                return None, "", f"guardrail block: {blocks}; rewrite"
            self.ctx.free_text.append({"question": label, "answer": value})
        return value, src, None

    async def t_fill_batch(self, fields: list[dict]):
        plan, results = [], []
        snap = self.ctx.last_snapshot
        for f in fields:
            item = snap.find(f.get("ref", "")) if snap else None
            if (f.get("source") or "").lower() == "as_is":
                # Surya set this field himself in Chrome (or answered "I already filled it out"): what the page
                # shows IS his decision. Record it unchanged so the gate sees a sourced value (2026-10-18).
                try:
                    info = await self.ctx.ex.read_ref(f.get("ref", ""))
                except Exception:
                    info = {}
                cur = str(info.get("value") or "") if info.get("found") else ""
                # plain inputs are judged by their value; only widget-style controls (react-select, Workday buttons) by the text they show
                widgety = str(info.get("tag") or "").lower() not in ("input", "textarea", "select") or info.get("expanded") is not None
                if cur in ("", "unchecked") and not (widgety and info.get("shown") and not re.match(r"^(select|select\.\.\.|select one|choose)$", str(info.get("shown")).strip(), re.I)):
                    results.append({"ref": f.get("ref"), "status": "failed", "error": "the field is empty: nothing to keep as-is; set it from an answer_key or ask_user"})
                    continue
                shown = cur if cur not in ("", "unchecked") else str(info.get("shown"))[:120]
                records.record_decision(self.ctx.job_id, self.ctx.page_no, f.get("label") or item.get("label", "") if item else f.get("label", ""),
                                        shown, "user:manual", f.get("ref", ""))
                results.append({"ref": f.get("ref"), "status": "kept", "readback": shown[:120]})
                continue
            is_choice = bool(item and (item.get("type") in ("radio", "checkbox") or item.get("tag") in ("select", "dropdown", "toggle")))
            if item and item.get("tag") == "toggle" and f.get("answer_key"):
                # A toggle option is pressed only when its own label agrees with the stored answer.
                a = answers.get(f["answer_key"], self.ctx.job.get("company", ""), self.ctx.portal)
                if a and a.has_value:
                    ans = answers.norm(str(a.answer)); lab = answers.norm(item.get("label") or "")
                    agree = (lab == ans) or (ans in ("yes", "true") and lab in ("yes", "true")) or (ans == "no" and lab in ("no", "false")) \
                        or (ans in ("yes", "true") and not NEGATED.match(item.get("label") or "") and lab not in ("no", "false")) \
                        or (ans == "no" and bool(NEGATED.match(item.get("label") or "")))
                    if not agree:
                        results.append({"ref": f["ref"], "status": "failed", "error": f"stored answer for {f['answer_key']} is {a.answer!r} but this toggle reads {item.get('label')!r}; press the matching option instead"})
                        continue
                    f = {**f, "value": "true", "_force_true": True}
            if is_choice and not f.get("answer_key") and (f.get("source") or "").lower() == "generated":
                results.append({"ref": f["ref"], "status": "failed", "error": "choices (radio/checkbox/select) need an answer_key: call lookup_answer, or ask_user if none fits"})
                continue
            val, src, err = self._resolve_value(f.get("answer_key"), f.get("value"), f.get("source"), f.get("label", ""))
            if err:
                results.append({"ref": f["ref"], "status": "failed", "error": err})
                continue
            if f.get("_force_true"):
                val = "true"
            # An ordered preference list ("LinkedIn; Job Board; Agency") is for pickers. A text box
            # takes the first choice only, never the whole chain.
            if f.get("answer_key") and ";" in str(val) and item and item.get("tag") in ("input", "textarea", "richtext") \
                    and item.get("type") not in ("radio", "checkbox"):
                a = answers.get(f["answer_key"], self.ctx.job.get("company", ""), self.ctx.portal)
                if a is not None and a.answer_type == "choice_chain":
                    val = str(val).split(";")[0].strip()
            # Checkbox/radio with a text answer: check it when the option's own label matches the answer
            # ("Available May 2027" with answer "May 2027"); refuse when it plainly does not.
            label_matched = False
            if is_choice and item and item.get("type") in ("radio", "checkbox") and str(val).strip().lower() not in AFFIRMATIVE | {"false", "no", "off", "unchecked"}:
                lab = answers.norm(item.get("label") or ""); nv = answers.norm(str(val))
                parts = [answers.norm(p) for p in str(val).split(";") if answers.norm(p)]
                if nv and (nv == lab or (len(nv) >= 4 and nv in lab) or any(p == lab or (len(p) >= 4 and p in lab) for p in parts)):
                    val = "true"
                    # The stored text names this option ("Not Specified"), negated or not: only when the label
                    # starts with it, so "willing" never ticks "Not willing to relocate".
                    label_matched = any(lab.startswith(x) for x in [nv, *parts] if x)
                elif item.get("type") == "checkbox" and parts and parts[0] in SELECT_ALL and not NEGATED.match((item.get("label") or "").strip()):
                    # "All offered locations; ..." on a multi-select group: every box is ticked (Surya, 2026-10-18)
                    val = "true"
                    label_matched = True
                elif str(f.get("answer_key") or "").startswith("preferences.") and not NEGATED.match((item.get("label") or "").strip()):
                    # A team / organization / track pick is a delegated preference, not a fact: company-specific names
                    # ("Platform Services", "Foundations") never match the stored list, and the model picks the most
                    # backend-flavored ones under the preference key (Surya, 2026-10-18).
                    val = "true"
                    label_matched = True
                elif item.get("type") == "checkbox" and parts and parts[0] in AFFIRMATIVE and not NEGATED.match((item.get("label") or "").strip()):
                    # "Yes; I agree; Accept" on a single consent checkbox: its first choice is a plain yes.
                    val = "true"
                else:
                    results.append({"ref": f["ref"], "status": "failed",
                                    "error": f"stored answer {str(val)[:40]!r} does not match this option's label {item.get('label', '')[:40]!r}; pick the option whose label matches, or leave it"})
                    continue
            # A "Yes" answer must not land on a negated option ("Not willing to relocate").
            if is_choice and item and item.get("type") in ("radio", "checkbox") and not label_matched:
                opt_label = (item.get("label") or "")
                if str(val).strip().lower() in AFFIRMATIVE and NEGATED.match(opt_label.strip()):
                    results.append({"ref": f["ref"], "status": "failed",
                                    "error": f"the stored answer is Yes but this option reads '{opt_label[:50]}' (negated); pick the option whose label matches the answer, or leave it unchecked"})
                    continue
            # A capped box (maxlength or a "Maximum N characters" hint) silently cuts longer text: refuse it.
            cap = int(item.get("max_chars") or 0) if item and not is_choice else 0
            if cap and len(str(val)) > cap:
                results.append({"ref": f["ref"], "status": "failed",
                                "error": f"too long: {len(str(val))} characters, and this box allows {cap}. Shorten it to at most {cap} "
                                         "characters (keep the facts; guardcheck it again), then fill it again"})
                continue
            plan.append({"ref": f["ref"], "value": val, "_src": src, "_label": f.get("label", ""), "_text": not is_choice})
        if plan:
            res = await fill.fill_batch(self.ctx.ex, [{"ref": p["ref"], "value": p["value"]} for p in plan])
            by = {r.ref: r for r in res}
            for p in plan:
                r = by.get(p["ref"])
                d = r.as_dict() if r else {"ref": p["ref"], "status": "failed", "error": "no result"}
                results.append(d)
                if r and r.ok:
                    records.record_decision(self.ctx.job_id, self.ctx.page_no, p["_label"], r.readback, p["_src"], p["ref"])
        self.ctx.budget.step()
        set_n = sum(1 for r in results if r.get("status") == "set")
        return {"summary": f"{set_n} set, {len(results) - set_n} failed", "results": results}, None

    SENSITIVE_KEYS = re.compile(r"^(self_id\.|compliance\.(date_of_birth|criminal|disciplined|debarment)|bank\.(gender|race|ethnic|veteran|disab|transgender|sexual))", re.I)

    async def t_choose(self, ref: str, answer_key: str | None, options: list[str], label: str, source: str | None):
        if answer_key and self.SENSITIVE_KEYS.match(answer_key) and options:
            # Surya's self-identification answers are his stored chain and nothing else; a model-supplied
            # fallback ("Asian (...)" next to "Decline to self-identify") is never tried.
            db.event(self.ctx.job_id, "answer:self_id_guard", {"key": answer_key, "dropped": [str(o)[:60] for o in options][:6], "label": label[:80]})
            options = []
        wants = list(options or [])
        src = "adapter"
        if answer_key:
            val, src, err = self._resolve_value(answer_key, None, None, label)
            if err:
                return {"error": err}, None
            wants = [w.strip() for w in str(val).split(";") if w.strip()] + wants
            # Portal spellings of the same answer ("Texas A & M University" on Workday) live in <key>_variants.
            variants = answers.get(answer_key + "_variants", self.ctx.job.get("company", ""), self.ctx.portal)
            if variants and variants.has_value:
                wants += [w.strip() for w in str(variants.answer).split(";") if w.strip() and w.strip() not in wants]
        elif not wants:
            return {"error": "give options or an answer_key"}, None
        r = await fill.choose(self.ctx.ex, ref, wants)
        self.ctx.budget.step()
        if r.ok:
            records.record_decision(self.ctx.job_id, self.ctx.page_no, label, r.readback, src if answer_key is None else src, ref)
            self.ctx.control_failures.pop(ref, None)
            out = r.as_dict()
            first = next((w for w in wants if w), "")
            if first and answers.norm(str(r.readback)) != answers.norm(first):
                out["note"] = (f"{str(r.readback)[:60]!r} is this portal's wording for {first[:40]!r} (one of the stored variants): "
                               "it is the correct, sourced value. Do not re-set it, do not ask, do not park over it.")
            return out, None
        return self._with_fallback_hint(ref, r.as_dict()), None

    def _with_fallback_hint(self, ref: str, out: dict) -> dict:
        """Surya (2026-10-18): when the ref tools cannot work a control, go to screenshot mode and click it by
        hand. After the second failure on the same ref the model is told to switch to the computer tool."""
        n = self.ctx.control_failures.get(ref, 0) + 1
        self.ctx.control_failures[ref] = n
        if n >= 2:
            out["fallback"] = ("the ref tools failed on this control twice: switch to the `computer` tool now. Take a screenshot, "
                              "click the control, type or scroll inside its list, click the wanted option, then `snapshot` to confirm the "
                              "value and record it with fill_batch source 'as_is' if the outline shows it set.")
        return out

    async def t_click(self, ref: str, label: str):
        snap = self.ctx.last_snapshot
        item = snap.find(ref) if snap else None
        if item and item.get("submitish"):
            return {"error": "that is a submit control; call submit instead"}, None
        if item and SOCIAL_SIGNIN.search(item.get("label") or label or ""):
            return {"error": "never sign in through Google, LinkedIn, Apple, Microsoft or Facebook. Click 'Sign in with email' "
                             "(or the email / password form), then call `credentials`."}, None
        if item and (item.get("type") in ("radio", "checkbox") or item.get("tag") in ("select", "dropdown", "toggle")):
            return {"error": "that is a choice control; set it with fill_batch (radio/checkbox/toggle, value from an answer_key) or choose (dropdown) so the decision has a source"}, None
        info = await self.ctx.ex.click_ref(ref)
        await self.ctx.ex.settle(quiet_ms=400)
        note = await self.ctx.ex.follow_new_tab()
        self.ctx.budget.step()
        new_url = self.ctx.ex.url
        if info.get("tag") in ("button", "a") and label.lower().strip() in ("next", "continue", "save and continue", "save & continue", "save", "proceed"):
            self.ctx.page_no += 1
        out = {"clicked": info.get("label") or label, "url": new_url}
        if note:
            out["note"] = note
        return out, None

    JS_RAIL_STEP = r"""(name) => {
      const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
      const want = norm(name);
      const cands = Array.from(document.querySelectorAll('[data-automation-id*="progressBar"] *, [data-automation-id*="progress"] *, nav *, [role="list"] *, [role="listitem"] *, li, div, span, button, a'))
        .filter(el => { const r = el.getBoundingClientRect(); return r.width > 20 && r.height > 8 && r.y < 320 && norm(el.textContent) === want; });
      if (!cands.length) return JSON.stringify({ ok: false });
      // the innermost match (deepest in the tree) is the label itself; climb to a clickable ancestor
      let el = cands[cands.length - 1];
      let click = el;
      for (let p = el, i = 0; p && i < 6; p = p.parentElement, i++) {
        const role = p.getAttribute && (p.getAttribute('role') || '');
        if (p.tagName === 'BUTTON' || p.tagName === 'A' || role === 'button' || role === 'link' || (p.getAttribute && p.getAttribute('tabindex') !== null) || (p.onclick)) { click = p; break; }
      }
      const r = click.getBoundingClientRect();
      return JSON.stringify({ ok: true, x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), tag: click.tagName });
    }"""

    async def t_go_to_step(self, step: str):
        import asyncio
        want = step.strip().lower()
        visits = self.ctx.control_failures.get(f"step:{want}", 0) + 1
        self.ctx.control_failures[f"step:{want}"] = visits
        if visits > 2:
            return {"error": f"you have already gone back to {step!r} twice this run and set its fields with sourced values; a wording that "
                             "differs from the stored one is the portal's own, which is correct. Do not go back again: continue forward to "
                             "Review and call submit."}, None

        async def current_step() -> str:
            text = await self.ctx.ex.page_text(4000)
            m = re.search(r"current step \d+ of \d+\s*\n\s*([^\n]+)", text, re.I)
            return (m.group(1).strip().lower() if m else "")

        if (await current_step()).startswith(want):
            return {"status": "already_there", "step": step}, None
        # 1. the rail label, when the tenant makes it clickable
        pos = await self.ctx.ex.eval(self.JS_RAIL_STEP, step)
        if isinstance(pos, dict) and pos.get("ok"):
            await self.ctx.ex.click_xy(int(pos["x"]), int(pos["y"]))
            await asyncio.sleep(1.5)
        # 2. otherwise walk back one page at a time with the Back button (never Save and Continue: that moves forward)
        for _ in range(7):
            if (await current_step()).startswith(want):
                break
            snap = await snapmod.take(self.ctx.ex)
            back = next((it for it in snap.items if (it.get("label") or "").strip().lower() == "back" and it.get("tag") == "button"), None)
            if back is None:
                break
            try:
                await self.ctx.ex.click_ref(back["ref"])
            except Exception:
                break
            await asyncio.sleep(1.5)
        self.ctx.budget.step()
        snap = await snapmod.take(self.ctx.ex)
        self.ctx.last_snapshot = snap
        now = await current_step()
        ok = now.startswith(want)
        db.event(self.ctx.job_id, "nav:step", {"step": step, "ok": ok, "current": now[:60]})
        if not ok:
            return {"status": "not_reached", "current_step": now, "hint": "use the Back button yourself, one page at a time, reading the 'current step' line after each click"}, None
        return {"status": "moved", "current_step": now, "note": "snapshot taken; fill the section, then Save and Continue forward through each step to Review"}, None

    async def t_tabs(self, action: str, index: int | None = None, url: str | None = None):
        ex = self.ctx.ex
        if action == "list":
            return {"tabs": await ex.list_tabs()}, None
        if action == "switch":
            if index is None:
                return {"error": "give the tab index (from action 'list')"}, None
            out = await ex.switch_tab(int(index))
        elif action == "close":
            if index is None:
                return {"error": "give the tab index (from action 'list')"}, None
            out = await ex.close_tab(int(index))
        elif action == "back":
            await ex.page.go_back(wait_until="domcontentloaded", timeout=20000)
            await ex.settle(quiet_ms=400)
            out = {"url": ex.url}
        elif action == "reload":
            await ex.page.reload(wait_until="domcontentloaded", timeout=30000)
            await ex.settle(quiet_ms=400)
            out = {"url": ex.url}
        elif action == "goto":
            if not url:
                return {"error": "give the url"}, None
            out = {"url": await ex.goto(url)}
        else:
            return {"error": f"unknown action {action}"}, None
        self.ctx.budget.step()
        self.ctx.last_snapshot = None   # a different page: snapshot again before using refs
        out["note"] = "call snapshot before using any ref on this page"
        return out, None

    async def t_upload_resume(self, ref: str, document: str):
        p = _resume_path(self.ctx, document)
        if not p or not p.exists():
            return {"error": f"{document} file not available in the résumé folder"}, None
        if self.ctx.resume_file == p.name and document == "resume":
            return {"ref": ref, "status": "set", "readback": p.name,
                    "note": "already attached in this run; do not upload it again"}, None
        chooser = self.ctx.ex.pending_chooser
        if chooser is not None and (not self.ctx.last_snapshot or not self.ctx.last_snapshot.find(ref)):
            # The page opened a file picker (a custom upload button): answer the picker directly.
            await chooser.set_files(str(p))
            self.ctx.ex.pending_chooser = None
            await self.ctx.ex.settle(quiet_ms=600)
            r = fill.FieldResult(ref, await fill._shows_file(self.ctx.ex, p), p.name)
        else:
            r = await fill.upload_file(self.ctx.ex, ref, p)
            self.ctx.ex.pending_chooser = None
        self.ctx.budget.step()
        if r.ok:
            if document == "resume":
                self.ctx.resume_file = p.name
            records.record_decision(self.ctx.job_id, self.ctx.page_no, f"{document} upload", p.name, "resume", ref)
        return r.as_dict(), None

    async def t_set_date(self, ref: str, answer_key: str | None, value: str | None, label: str):
        src = "adapter"
        if answer_key:
            val, src, err = self._resolve_value(answer_key, None, None, label)
            if err:
                return {"error": err}, None
        elif value:
            val = value
            src = "resume"
        else:
            return {"error": "give a value or an answer_key"}, None
        r = await fill.set_date(self.ctx.ex, ref, str(val))
        self.ctx.budget.step()
        if r.ok:
            records.record_decision(self.ctx.job_id, self.ctx.page_no, label, r.readback or str(val),
                                    f"standing:{answer_key}" if answer_key else src, ref)
        out = r.as_dict()
        return (out if out.get('status') in ('set', 'ok') or out.get('ok') else self._with_fallback_hint(ref, out)), None

    async def t_zoom(self, x: int, y: int, width: int, height: int):
        png = await self.ctx.ex.zoom_png(x, y, width, height)
        return {"zoom": f"crop ({x},{y}) {width}x{height} attached"}, png

    def _present(self, a, via: str | None = None, confidence: float | None = None) -> dict:
        val, why = answers.concrete_value(a, company=self.ctx.job.get("company", ""), posting_text=self.ctx.posting_text,
                                          listing_id=self.ctx.job.get("listing_id"))
        out = {"key": a.key, "answer": val, "confidence": a.confidence if confidence is None else confidence,
               "via": via or a.via, "note": a.note}
        if a.is_blank:
            out["note"] = "leave this field empty; if the form requires it, park with reason 'required field Surya leaves blank: <label>'"
        elif why:
            out["why"] = why
        return out

    async def _settle_affiliation(self, question: str, options: list[str]) -> dict:
        """Employer / industry / affiliation / relationship yes-no questions: answered from the profile,
        default No (Surya, 2026-10-15), saved so the decision is remembered and reviewable."""
        facts = profile.affiliations_brief()
        if self.ctx.classifier is not None and facts:
            ans, why = await self.ctx.classifier.yes_no_from_profile(self.ctx.job_id, question, options, facts)
        else:
            ans, why = "No", "default No"
        key = answers.key_from_question(question)
        answers.save_answer(key, ans, answer_type="choice", source="default_no" if ans == "No" else "profile",
                            note=f"auto from profile: {why[:150]}", aliases=[question])
        db.event(self.ctx.job_id, "answer:auto_affiliation", {"key": key, "answer": ans, "why": why[:200]})
        return {"key": key, "answer": ans, "confidence": 1.0, "via": "profile_default_no", "note": f"answered from the profile: {why[:120]}"}

    async def _kb_resolve(self, question: str, options: list[str], field_type: str = "", meaning: str | None = None,
                          ref: str | None = None) -> tuple[dict | None, dict]:
        """Ask the resolver whether the knowledge base answers this question. Returns (presented answer or
        None, info). A derived answer (a fact the profile states plainly, with the quote) is saved to the
        answer store under a new key, so the gate sees a sourced decision and the question never comes back."""
        clf = self.ctx.classifier
        nq = answers.norm(question)
        if clf is None or not hasattr(clf, "resolve"):
            return None, {}
        if nq in self.ctx.resolved:
            return self.ctx.resolved[nq]
        company, portal = self.ctx.job.get("company", ""), self.ctx.portal
        opts = [str(o) for o in (options or [])]
        png = None
        if ref:
            try:
                seen, png = await fill.peek_options(self.ctx.ex, ref)
                opts = list(dict.fromkeys(opts + [s for s in seen if s]))
            except Exception as e:
                log.debug("peek_options failed: %s", e)
        if png is None:
            try:
                png = await self.ctx.ex.screenshot_png()
            except Exception:
                png = None
        from .knowledge import knowledge_text
        out = await clf.resolve(self.ctx.job_id, question, opts, meaning=meaning, field_type=field_type,
                                knowledge=knowledge_text(company, portal), image_png=png)
        info = {"meaning": (out or {}).get("meaning"), "choices": opts}
        result: tuple[dict | None, dict] = (None, info)
        if out:
            verdict, conf = out.get("verdict"), float(out.get("confidence") or 0)
            evidence = str(out.get("evidence") or "").strip()
            if verdict == "stored_key" and out.get("key") and conf >= SETTINGS.models.resolver_min_confidence_key:
                a = answers.get(str(out["key"]), company, portal)
                if a is not None and a.has_value:
                    answers.record_classifier_alias(question, a.key, conf)
                    pres = self._present(a, via="resolver", confidence=conf)
                    pres.update({"meaning": out.get("meaning"), "choices": opts})
                    result = (pres, info)
            elif (verdict == "derived" and out.get("answer") and evidence and not NO_DERIVE.search(question)
                  and conf >= SETTINGS.models.resolver_min_confidence_derived):
                val = str(out["answer"]).strip()
                if opts:
                    val = next((o for o in opts if answers.norm(o) == answers.norm(val)), "")
                key = answers.key_from_question(question)
                existing = answers.get(key, company, portal)
                if val and not (existing is not None and existing.scope == "company"):
                    answers.save_answer(key, val, answer_type="choice" if opts else "text", source="derived",
                                        source_ref=f"job:{self.ctx.job_id}",
                                        note=f"{str(out.get('meaning') or '')[:120]} | evidence: {evidence[:240]}",
                                        aliases=[question])
                    db.event(self.ctx.job_id, "answer:derived", {"key": key, "question": question[:200], "answer": val[:200],
                                                                 "evidence": evidence[:300], "confidence": conf})
                    a = answers.get(key, company, portal)
                    if a is not None:
                        pres = self._present(a, via="derived", confidence=conf)
                        pres.update({"meaning": out.get("meaning"), "evidence": evidence[:300], "choices": opts})
                        result = (pres, info)
        self.ctx.resolved[nq] = result
        return result

    async def t_lookup_answer(self, question: str, options: list[str], meaning: str | None = None, ref: str | None = None):
        a = answers.lookup(question, self.ctx.job.get("company", ""), self.ctx.portal)
        if a is not None and a.has_value:
            return self._present(a), None
        if a is not None and a.scope == "company":
            return {"miss": True, "key": a.key, "hint": f"Surya decides '{a.key}' himself for {self.ctx.job.get('company')}: call ask_user with this question"}, None
        ck = consent_question_key(question) if (options or not is_free_response(question, options)) else None
        if ck:
            a3 = answers.get(ck, self.ctx.job.get("company", ""), self.ctx.portal)
            if a3 and a3.has_value:
                out = self._present(a3, via="consent_rule")
                out["note"] = "Surya consents on every form (his standing rule); pick the option that gives consent" if ck != "consent.marketing_email" \
                    else "marketing / talent-community opt-in: Surya always declines"
                return out, None
        if is_affiliation_question(question, options):
            return await self._settle_affiliation(question, options), None
        if is_free_response(question, options):
            return {"miss": True, "hint": "free-response: draft it yourself from the PROFILE under the drafting rules, guardcheck it, "
                                          "and fill it with source 'generated'; Surya is never asked to write prose"}, None
        if self.ctx.classifier is not None and hasattr(self.ctx.classifier, "resolve"):
            pres, info = await self._kb_resolve(question, options, "", meaning, ref)
            if pres is not None:
                return pres, None
            return {"miss": True, "meaning": info.get("meaning"), "choices": info.get("choices"),
                    "hint": "the knowledge base does not settle this; if the field is required call ask_user (with these choices), "
                            "if optional leave it blank"}, None
        if self.ctx.classifier is not None:
            con = db.connect()
            keys = [(r["key"], str(r["answer"])) for r in con.execute(
                "SELECT key, answer FROM answers WHERE superseded_by IS NULL AND approved=1 AND answer IS NOT NULL AND scope='global' "
                "AND key NOT LIKE 'story.%' AND key NOT LIKE 'always_hold.%' ORDER BY key")]
            key, conf = await self.ctx.classifier.map_question(self.ctx.job_id, question, options, keys)
            if key and conf >= SETTINGS.models.classifier_min_confidence:
                a2 = answers.get(key, self.ctx.job.get("company", ""), self.ctx.portal)
                if a2 and a2.has_value:
                    answers.record_classifier_alias(question, key, conf)
                    return self._present(a2, via="classifier", confidence=conf), None
            return {"miss": True, "hint": "no stored answer; if the field is required call ask_user, if optional leave it blank",
                    "best_guess_key": key, "confidence": conf}, None
        return {"miss": True, "hint": "no stored answer; if the field is required call ask_user, if optional leave it blank"}, None

    async def t_ask_user(self, questions: list[dict]):
        qs, auto, draft = [], [], []
        for q in questions:
            text, opts, ftype = q["question"], q.get("options") or [], q.get("field_type") or ""
            # Never ask Surya to write prose: free-response questions are drafted from the profile.
            if is_free_response(text, opts, ftype):
                draft.append(text)
                continue
            # Consents and acknowledgements: Surya consents on every form; marketing opt-ins are declined.
            ck = consent_question_key(text)
            if ck and (opts or not is_free_response(text, opts, ftype)):
                a3 = answers.get(ck, self.ctx.job.get("company", ""), self.ctx.portal)
                if a3 and a3.has_value:
                    auto.append(self._present(a3, via="consent_rule"))
                    continue
            # Employer / industry / affiliation yes-no questions settle from the profile, default No.
            if is_affiliation_question(text, opts):
                auto.append(await self._settle_affiliation(text, opts))
                continue
            # The knowledge base first (the resolver result from lookup_answer is reused, not recomputed).
            pres, info = await self._kb_resolve(text, opts, ftype, None, q.get("ref"))
            if pres is not None:
                auto.append(pres)
                continue
            if info.get("choices") and not opts:
                opts = info["choices"]       # Surya gets the real choices with the question
            # A picker with exactly one real choice is an acknowledgement ("Thank you" under Lyft's "enter your
            # employment above"): take it; it is not a question for Surya (2026-10-18).
            real = [o for o in opts if o and not re.match(r"^(select|select\.\.\.|select one|choose|choose one|please select|-+)$", str(o).strip(), re.I)]
            if len(real) == 1 and (opts or ftype.lower() in ("select", "dropdown", "combobox", "radio")):
                key = answers.key_from_question(text)
                answers.save_answer(key, str(real[0]), answer_type="choice", source="only_option",
                                    note="the field offered one choice; taken as an acknowledgement", aliases=[text])
                a1 = answers.get(key, self.ctx.job.get("company", ""), self.ctx.portal)
                if a1 is not None:
                    pres1 = self._present(a1, via="only_option")
                    pres1["note"] = "the only choice the field offers; fill it with this key"
                    auto.append(pres1)
                    db.event(self.ctx.job_id, "answer:only_option", {"key": key, "question": text[:160], "answer": str(real[0])[:80]})
                    continue
            key = answers.key_from_question(text)
            qs.append({"key": key, "question": text, "options": opts, "field_type": ftype, "ref": q.get("ref")})
        if not qs:
            return {"asked": [], "auto_answered": auto, "draft_yourself": draft,
                    "note": "nothing was sent to Surya. Fill auto_answered with their keys. Draft each draft_yourself question from the "
                            "PROFILE under the drafting rules, guardcheck it, and fill it with source 'generated'."}, None
        png = None
        try:
            png = await self.ctx.ex.screenshot_png()
        except Exception:
            pass
        keys = await BROKER.ask(self.ctx.job_id, qs, png)
        self.ctx.asked_keys += [k for k in keys if k not in self.ctx.asked_keys]
        with db.tx() as con:
            con.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=? AND status=?",
                        (states.AWAITING_ANSWER, db.now(), self.ctx.job_id, states.FILLING))
        out = {"asked": keys, "note": "keep filling; call await_answers with these keys when the page is otherwise done"}
        if auto:
            out["auto_answered"] = auto
        if draft:
            out["draft_yourself"] = draft
            out["note"] += ". Draft each draft_yourself question from the PROFILE (source 'generated'); Surya is never asked to write prose."
        return out, None

    async def t_await_answers(self, keys: list[str]):
        got = await BROKER.await_answers(self.ctx.job_id, keys)   # raises Parked on timeout
        with db.tx() as con:
            con.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=? AND status=?",
                        (states.FILLING, db.now(), self.ctx.job_id, states.AWAITING_ANSWER))
        return {"answers": got, "note": "use these keys in fill_batch / choose"}, None

    async def t_guardcheck(self, text: str):
        blocks, warns = guardrail_check(text)
        return {"result": "BLOCK" if blocks else ("WARN" if warns else "OK"), "blocks": blocks, "warns": warns, "chars": len(text)}, None

    async def t_credentials(self, mode: str, email_ref: str, password_ref: str, confirm_password_ref: str | None):
        if self.ctx.creds is None:
            return {"error": "credentials helper not configured (Bitwarden CLI)"}, None
        ex = self.ctx.ex
        host = self.ctx.portal_host
        email = SETTINGS.creds.application_email

        async def _type_into(ref: str, text: str, secret: bool) -> None:
            await ex.focus_ref(ref)
            await ex.page.keyboard.press("Control+a")
            await ex.page.keyboard.press("Backspace")
            if secret:
                await ex.type_secret(text)
            else:
                await ex.type_text(text)

        loop = __import__("asyncio").get_event_loop()
        # The helper is synchronous and calls the callbacks; bridge them onto the loop.
        import asyncio
        def type_email(v: str) -> None:
            if email_ref:
                asyncio.run_coroutine_threadsafe(_type_into(email_ref, v, False), loop).result(30)
        def type_pw(v: str) -> None:
            asyncio.run_coroutine_threadsafe(_type_into(password_ref, v, True), loop).result(30)
        status = await asyncio.to_thread(self.ctx.creds.fill_credentials, host, email, type_email, type_pw, mode)
        if mode == "set_password" and status == "exists_unknown":
            return {"status": "no_password_configured", "hint": "PASSWORD_BIT is not set; park with credentials_unknown"}, None
        if confirm_password_ref and status.startswith("filled"):
            def type_pw2(v: str) -> None:
                asyncio.run_coroutine_threadsafe(_type_into(confirm_password_ref, v, True), loop).result(30)
            await asyncio.to_thread(self.ctx.creds.fill_confirm_password, host, type_pw2)
        self.ctx.budget.step()
        records.record_decision(self.ctx.job_id, self.ctx.page_no, f"account {mode} ({host})", "(set)", "policy:may_create_accounts", password_ref)
        if status == "exists_unknown":
            raise Parked(f"credentials_unknown: account exists on {host} with no stored credential", "hold")
        return {"status": status, "email": email}, None

    async def t_enter_email_code(self, code_refs: list[str]):
        if self.ctx.mail is None:
            return {"error": "mail helper not configured (Gmail API)"}, None
        import asyncio
        with db.tx() as con:
            con.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (states.AWAITING_CODE, db.now(), self.ctx.job_id))
        after_ts = self.ctx.code_requested_at or (db.now() - 180)
        handle = await asyncio.to_thread(self.ctx.mail.fetch_code, self.ctx.portal, self.ctx.portal_host, after_ts)
        if handle is None:
            with db.tx() as con:
                con.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (states.FILLING, db.now(), self.ctx.job_id))
            return {"status": "not_received", "hint": "request a new code once if the page offers it, then call again; otherwise park with 'code_not_received'"}, None
        ex = self.ctx.ex
        loop = asyncio.get_event_loop()
        ok_box = {"ok": False}

        def _type(code: str) -> None:
            ok_box["ok"] = asyncio.run_coroutine_threadsafe(fill.type_code_into(ex, code_refs, code), loop).result(60)

        status = await asyncio.to_thread(handle.enter, _type)
        with db.tx() as con:
            con.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (states.FILLING, db.now(), self.ctx.job_id))
        self.ctx.budget.step()
        records.record_decision(self.ctx.job_id, self.ctx.page_no, "emailed verification code", "(set)", "policy:may_enter_emailed_verification_code")
        return {"status": status if ok_box["ok"] else "entered_unverified", "message_id": getattr(handle, "message_id", None)}, None

    async def t_open_password_reset_link(self):
        if self.ctx.mail is None:
            return {"error": "mail helper not configured (Gmail API)"}, None
        import asyncio
        after_ts = db.now() - 600
        fetch = getattr(self.ctx.mail, "fetch_password_reset_link", None)
        if fetch is None:
            return {"error": "the mail helper cannot fetch reset links"}, None
        res = await asyncio.to_thread(fetch, self.ctx.portal, self.ctx.portal_host, after_ts)
        status = getattr(res, "status", "not_received")
        if status == "reset_link" and getattr(res, "url", None):
            await self.ctx.ex.goto(res.url)
            self.ctx.budget.step()
            db.event(self.ctx.job_id, "creds:reset_link_opened", {"host": self.ctx.portal_host})
            return {"status": "opened", "url": self.ctx.ex.url,
                    "next": "snapshot, then credentials mode 'set_password' with the new-password and confirm refs (email_ref ''), click the save button, then sign in with login_universal"}, None
        if status == "host_mismatch":
            raise Parked("host_mismatch: the reset link pointed at another host", "hold")
        return {"status": status, "hint": "no reset email yet; if the page offers to resend, click it once and call again, else park credentials_unknown"}, None

    async def t_open_verification_link(self, timeout_s: float | None = None):
        if self.ctx.mail is None:
            return {"error": "mail helper not configured (Gmail API)"}, None
        import asyncio
        # Look back a day: an account created in an earlier attempt is still waiting on its email, and only
        # links on THIS portal's host (from this tenant) are ever accepted.
        after_ts = db.now() - 86400
        self.ctx.verify_link_tried = True
        res = await asyncio.to_thread(self.ctx.mail.fetch_verification_link, self.ctx.portal, self.ctx.portal_host, after_ts,
                                      timeout_s=timeout_s)
        status = getattr(res, "status", "not_received")
        if status == "verified_link" and getattr(res, "url", None):
            await self.ctx.ex.goto(res.url)
            self.ctx.budget.step()
            return {"status": "verified", "url": self.ctx.ex.url}, None
        if status == "host_mismatch":
            raise Parked("host_mismatch: verification link pointed at another host", "hold")
        return {"status": status}, None

    # The portal did not load or stalled (a spinner, a disabled Save and Continue): retried later, not held.
    NOT_LOADING = re.compile(r"(fail(ed|s)? to load|did not load|didn'?t load|not loading|still loading|is loading|stuck (on )?loading|"
                             r"spinner|blank page|page (is |was )?blank|timed? ?out|remains disabled|stays disabled|unresponsive)", re.I)

    ACCOUNT_LABEL = re.compile(r"^(create account|create an account|sign in|sign-in|log in|login|continue|next|save and continue|"
                               r"save & continue|verify|verify email|send code|resend|back|apply|apply now|apply manually)$", re.I)

    async def t_submit(self, button_label: str | None):
        ctx = self.ctx
        if button_label and self.ACCOUNT_LABEL.match(button_label.strip()):
            return {"error": f"'{button_label}' is a navigation / account button: press it with `click`. `submit` is only for the "
                             "application's final Submit button, once every page is filled."}, None
        snap = await snapmod.take(ctx.ex)
        ctx.last_snapshot = snap
        if any(i.get("type") == "password" for i in snap.items):
            return {"error": "this page has a password field: it is a sign-in or account page, not the application. Use `credentials` "
                             "for the email and password, then `click` Sign In / Create Account, and fill the application that follows."}, None
        page_text = await ctx.ex.page_text(30000)
        gate = run_gate(job=ctx.job, snapshot=snap, page_text=page_text, expected_resume=ctx.resume_file,
                        free_text_answers=[f["answer"] for f in ctx.free_text], portal=ctx.portal)
        ctx.gate_result = gate.as_dict()
        db.event(ctx.job_id, f"gate:{gate.result}", gate.as_dict())
        with db.tx() as con:
            con.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (states.GATED, db.now(), ctx.job_id))
        if gate.result == "captcha_present":
            raise Parked("ready_for_you: " + "; ".join(gate.reasons), "ready_for_you")
        if gate.result in ("automation_prohibited", "guardrail_block", "already_applied"):
            raise Parked(f"{gate.result}: " + "; ".join(gate.reasons)[:300], "hold")
        if gate.result == "blocked":
            with db.tx() as con:
                con.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (states.FILLING, db.now(), ctx.job_id))
            return {"status": "blocked", "gate": "blocked", "fix_these_then_call_submit_again": gate.reasons}, None
        if gate.result == "pass_shadow":
            ctx.submitted = {"status": "shadow", "gate": gate.as_dict()}
            raise Parked("shadow_mode: gate passed; stopping before the final click", "hold")
        res = await do_submit(ctx.ex, ctx.job_id, label=button_label or "")
        db.event(ctx.job_id, f"submit:{res.status}", res.as_dict())
        if res.status == "submitted":
            ctx.submitted = res.as_dict()
            return {"status": "submitted", "signal": res.signal, "note": "done; the worker records the ledger"}, None
        if res.status == "failed":
            ctx.budget.failed_submit()   # may raise BudgetExceeded
            with db.tx() as con:
                con.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (states.FILLING, db.now(), ctx.job_id))
            return {"status": "failed", "why": res.why, "note": "do not click again; fix a named blocker then call submit once more, or park"}, None
        with db.tx() as con:
            con.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (states.FILLING, db.now(), ctx.job_id))
        return {"status": "blocked", "fix_these_then_call_submit_again": res.why}, None

    SATISFIED = re.compile(r"(work authorization|authorized to work|sponsorship|citizen|bachelor|gpa|18 years|relocat|onsite|on-site|hybrid|remote|2028|rising senior|eligible to obtain|clearance eligib|"
                           r"\bsatisf|40 hours|full[- ]time|hours (per|a) week|\d+ weeks|start date|begin|beginning|available from|"
                           r"(may|june) \d{1,2},? 2027|summer 2027|weekend|in[- ]person|commut|related (field|discipline|major|area|degree)|computer science|local to|during the internship)", re.I)
    # Surya 2026-10-18: a car requirement is fine (he arranges one); "local / within N miles" is fine when the posting is
    # paid; a straight hardware role is not.
    DISQUALIFIERS = re.compile(r"(master'?s|ph\.?d|doctoral|graduate program|active (secret|top secret|ts/sci|clearance)|returning intern|co-?op|fall 2027|spring 2028|quant|"
                               r"unpaid|hardware (engineer|design|role)|(fpga|asic|rtl|verilog|vhdl|pcb)|circuit design|analog design)", re.I)

    _SEASON_MONTH = {"spring": 5, "summer": 8, "fall": 12, "autumn": 12, "winter": 12, "jan": 1, "january": 1, "feb": 2, "february": 2,
                     "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
                     "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12}

    @classmethod
    def grad_window_excludes(cls, reason: str) -> bool:
        """True when the park reason names a graduation window that does not contain May 2028 (his graduation):
        "Dec 2028 - Spring 2029" excludes him, "December 2027 - June 2028" and "2028 graduates" do not."""
        if not re.search(r"graduat|class of|degree (by|in|before)|completion date", reason, re.I):
            return False
        # the model often appends his own date ("...; Surya graduates May 2028"): only the posting's dates count
        posting = " ".join(part for part in re.split(r"[;.]", reason) if not re.search(r"\bsurya\b|\bhe\b|\bhis\b", part, re.I))
        dates = []
        for m in re.finditer(r"(?:\b([A-Za-z]{3,9})\.?,?\s+)?\b(20[2-3]\d)\b", posting):
            word, year = (m.group(1) or "").lower(), int(m.group(2))
            month = cls._SEASON_MONTH.get(word)
            dates.append((year, month))
        if not dates:
            return False
        lo = min((y, mo or 1) for y, mo in dates)
        hi = max((y, mo or 12) for y, mo in dates)
        return lo > (2028, 5) or hi < (2028, 5)

    async def t_park(self, reason: str):
        # A portal failure page (Workday "Something went wrong ... Error Code") cannot be fixed on the form:
        # park it as a portal error at once; the watchdog retries it in a fresh tab later.
        from ..gate import portal_error
        try:
            perr = portal_error(await self.ctx.ex.page_text(30000))
        except Exception:
            perr = None
        if perr:
            db.event(self.ctx.job_id, "park:portal_error", {"reason": reason[:200], "page": perr})
            raise Parked(f"portal_error: {perr}", "hold")
        state = "ready_for_you" if reason.lower().startswith("ready_for_you") else "hold"
        low = reason.lower()
        if low.startswith("eligibility") and re.search(r"within \d+ miles|commut|local (candidates|to)|relocation (assistance|help)", low)                 and not re.search(r"unpaid|no pay|volunteer", low) and self.ctx.park_attempts < 2:
            # A local-candidates requirement only matters when the role is unpaid (Surya, 2026-10-18).
            from ..answers import posting_pay
            if posting_pay(self.ctx.posting_text) or posting_pay(await self.ctx.ex.page_text(30000)):
                self.ctx.park_attempts += 1
                db.event(self.ctx.job_id, "park:bounced", {"reason": reason[:200]})
                return {"error": "not parked: the posting lists pay, so a local / commuting-distance requirement is fine (he relocates "
                                 "for a paid summer at his own expense). Continue filling; relocation questions take their answer_keys."}, None
        # A park that names a field already set this attempt (a chip in the portal's own wording) is bounced with
        # the recorded value: the harness verified it, the wording is the portal's (2026-10-18).
        if self.ctx.park_attempts < 3:
            try:
                decs = records.decisions(self.ctx.job_id)
            except Exception:
                decs = []
            words = {w for w in re.findall(r"[a-z]{4,}", low)}
            named = [d for d in decs if d.get("field_label") and len({w for w in re.findall(r"[a-z]{4,}", str(d["field_label"]).lower())} & words) >= 2
                     and str(d.get("source") or "").startswith(("standing:", "bank:", "adapter", "resume", "user:"))]
            if named and not low.startswith(("ready_for_you", "eligibility", "credentials", "code_not", "host_mismatch", "bot_check", "automation_prohibited")):
                self.ctx.park_attempts += 1
                shown = "; ".join(f"{d['field_label'][:40]} = {str(d.get('value'))[:50]!r}" for d in named[:3])
                db.event(self.ctx.job_id, "park:bounced", {"reason": reason[:200], "set_fields": shown[:200]})
                return {"error": f"not parked: the field(s) this names are already set and sourced this attempt ({shown}); a wording that differs "
                                 "from the stored value is the portal's own spelling, which is correct. Move on to the rest of the page and submit."}, None
        if re.search(r"race|ethnicit|hispanic|latino|gender|veteran|disabilit|self[- ]identif", low) and self.ctx.park_attempts < 3:
            # Self-identification never parks: every wording has a stored chain, including his own answers for forms
            # with no decline option (Surya, 2026-10-18).
            self.ctx.park_attempts += 1
            db.event(self.ctx.job_id, "park:bounced", {"reason": reason[:200], "self_id": True})
            return {"error": "not parked: self-identification fields are settled by their self_id.* keys (choose with the key; the chain ends with "
                             "his own answers for forms without a decline option). Whatever the picker shows after that is correct. Move on: "
                             "finish the other sections (Work Experience via go_to_step('My Experience') if the Review shows No Response) and submit."}, None
        if low.startswith("credentials") and self.ctx.park_attempts < 2:
            # The helper works (one application password): a credentials park needs a real sign-in failure on
            # the page. Wrong-password pages get the reset procedure; anything else gets told to use the helper.
            try:
                from ..creds.bitwarden import env_login_active
                helper_ok = env_login_active()
            except Exception:
                helper_ok = False
            if helper_ok:
                text = await self.ctx.ex.page_text(30000)
                self.ctx.park_attempts += 1
                db.event(self.ctx.job_id, "park:bounced", {"reason": reason[:200], "credentials": "helper available"})
                if re.search(r"wrong email address or password|account might be locked|incorrect (email|password)|invalid (email|password|credentials)", text, re.I):
                    return {"error": "not parked: this tenant holds his account under an older password. Click 'Forgot your password?', fill the "
                                     "email (answer_key identity.email), click the send button, call open_password_reset_link, snapshot, then "
                                     "credentials mode 'set_password' on the new-password boxes, click save, then credentials login_universal and Sign In."}, None
                return {"error": "not parked: the password helper is configured. On a Create Account page call credentials mode 'create' "
                                 "(it types the email, password and confirm box), click Create Account, then snapshot and continue; on a Sign In "
                                 "page call credentials login_universal, click Sign In, then snapshot. Park only after the page itself reports a failure."}, None
        if (low.startswith("credentials") or "verify your" in low or "verification" in low) and not self.ctx.verify_link_tried \
                and self.ctx.mail is not None:
            # An account that exists but cannot sign in is usually waiting on its verification email (a short
            # lookback: the email, if any, was sent earlier; never the full code wait here).
            try:
                out, _ = await self.t_open_verification_link(timeout_s=20)
            except Parked:
                out = {}
            if out.get("status") == "verified":
                db.event(self.ctx.job_id, "park:bounced", {"reason": reason[:200], "verified": True})
                return {"error": "not parked: opened the account verification link from this portal's email. Sign in again "
                                 "(credentials mode 'login_universal', then click Sign In) and continue the application."}, None
        pending = [k for k in self.ctx.asked_keys
                   if not ((a := answers.get(k, self.ctx.job.get("company", ""), self.ctx.portal)) and a.has_value)]
        if pending and self.ctx.park_attempts < 2 and not low.startswith(
                ("answer_", "ready_for_you", "bot_check", "automation_prohibited", "credentials", "code_not",
                 "host_mismatch", "eligibility", "shadow_mode", "portal_error", "needs_you")):
            # The model asked Surya and then gave up at once (GCM Grosvenor, 2026-10-18): the clock is 15 minutes.
            self.ctx.park_attempts += 1
            db.event(self.ctx.job_id, "park:bounced", {"reason": reason[:200], "pending": pending[:6]})
            return {"error": f"not parked: {len(pending)} question(s) are with Surya ({', '.join(pending[:4])}). Finish everything else on "
                             "the page, then call await_answers with those keys; the run parks by itself if he does not answer in time."}, None
        stalled = bool(self.NOT_LOADING.search(reason)) and not low.startswith(
            ("answer_", "ready_for_you", "eligibility", "bot_check", "credentials", "host_mismatch", "code_not", "shadow_mode"))
        if stalled and self.ctx.park_attempts >= 1:
            raise Parked(f"portal_error: the portal did not load or stalled ({reason[:150]})", "hold")
        if stalled:
            self.ctx.park_attempts += 1
            db.event(self.ctx.job_id, "park:bounced", {"reason": reason[:200]})
            return {"error": "not parked yet: reload once (navigate back to the posting and click Apply / Apply Manually again, or "
                             "snapshot after a short wait). If it still does not load, park again with the same reason."}, None
        if low.startswith("automation_prohibited") and self.ctx.park_attempts >= 2:
            from ..gate import scan_page_prohibitions
            if not scan_page_prohibitions(await self.ctx.ex.page_text(30000)):
                reason = "stuck: " + reason.split(":", 1)[-1].strip()      # never label a hold "prohibited" without the page saying so
                low = reason.lower()
        if self.ctx.park_attempts < 2 and (low.startswith("automation_prohibited") or "password" in low or "credential" in low or "create account" in low or "sign in" in low):
            # "Prohibited" must be on the page, not inferred; account pages are handled by the credentials tool.
            from ..gate import scan_page_prohibitions
            text = await self.ctx.ex.page_text(30000)
            hits = scan_page_prohibitions(text)
            if not hits:
                self.ctx.park_attempts += 1
                db.event(self.ctx.job_id, "park:bounced", {"reason": reason[:200]})
                return {"error": "not parked: the page text contains no prohibition on automated or AI-assisted applications, and nothing here is "
                                 "forbidden. On a Sign In / Create Account page call `credentials` (mode 'login' first, 'create' if the account does not exist) "
                                 "with the email and password field refs; the helper fills them and you may click Create Account / Sign In (those are not "
                                 "submit controls). Then continue filling."}, None
            reason = "automation_prohibited: " + "; ".join(hits)[:300]
        if low.startswith("eligibility") and self.SATISFIED.search(reason) and not self.DISQUALIFIERS.search(reason) \
                and not self.grad_window_excludes(reason) and self.ctx.park_attempts < 2:
            self.ctx.park_attempts += 1
            db.event(self.ctx.job_id, "park:bounced", {"reason": reason[:200]})
            return {"error": "not parked: that requirement is SATISFIED by the standing facts (U.S. citizen, authorized, no sponsorship, "
                             "Bachelor's in progress, GPA 4.0, relocates anywhere, graduates May 2028). A posting stating it is not a restriction. "
                             "Continue: open the application (click Apply / Apply Manually) and fill it."}, None
        hard = reason.lower().startswith(("ready_for_you", "bot_check", "automation_prohibited", "eligibility", "answer_", "credentials", "code_not", "host_mismatch", "needs_you"))
        if not hard and self.ctx.park_attempts < 2:
            # An eager park: run the gate first. If it passes, the right move is submit; if it blocks,
            # the model gets the concrete blockers instead of giving up.
            self.ctx.park_attempts += 1
            snap = await snapmod.take(self.ctx.ex)
            self.ctx.last_snapshot = snap
            page_text = await self.ctx.ex.page_text(30000)
            gate = run_gate(job=self.ctx.job, snapshot=snap, page_text=page_text, expected_resume=self.ctx.resume_file,
                            free_text_answers=[f["answer"] for f in self.ctx.free_text], portal=self.ctx.portal)
            db.event(self.ctx.job_id, "park:checked", {"reason": reason[:200], "gate": gate.as_dict()})
            if gate.passed:
                return {"error": "not parked: the code gate PASSES on this page. Call submit now (the gate decides; in shadow mode it stops before the click)."}, None
            if gate.result == "blocked":
                return {"error": "not parked yet: the gate names concrete blockers. Fix them, then call submit; park only if one truly has no stored answer.",
                        "blockers": gate.reasons}, None
        raise Parked(reason, state, blocked_on=pending if state == "hold" else None)
