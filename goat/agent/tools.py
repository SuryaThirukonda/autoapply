"""Function tools the model can call, and the dispatcher that runs them. Every consequential action
lives here as deterministic code; the model only requests it. Secrets never pass through."""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import answers, db, profile, records, states
from ..budget import Budget, BudgetExceeded, Parked
from ..browser import fill, js, snapshot as snapmod
from ..browser.captcha import detect_challenge
from ..browser.executor import ClickRefused, Executor, NavigationRefused
from ..browser.submit import submit as do_submit
from ..config import SETTINGS
from ..gate import guardrail_check, has_submit_control, run_gate
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
         "source": {"type": ["string", "null"], "description": "required with a literal: 'generated' for free-response text you wrote under the writing rules, 'resume' for a value copied verbatim from the résumé text, 'clear' with an empty value to blank a field holding a stale or wrong value, 'computer' (value null, WITH the answer_key) after you picked the option with the computer tool: it is kept only when the page shows that answer, 'as_is' (value null) to keep what the field already shows because Surya set it himself in the browser or told you it is done"},
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
    {"type": "function", "name": "fill_sections", "strict": True,
     "description": "Workday My Experience page, or a Greenhouse page with an Employment section: deterministically add every stored work-history entry (title, company, location, dates, current flag, full role description) and every website, Add/Add Another included, skipping entries already present. Returns added / updated / failed / skipped. Call it before touching Work Experience or Websites by hand; fix only what it reports failed.",
     "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
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
    creds_mode: str = ""           # the last credentials mode filled (login / login_universal / create / set_password)
    reset_needed: bool = False     # a Sign In with the universal password failed on this tenant
    reset_link_tried: bool = False # open_password_reset_link was called this run
    reset_not_received: bool = False  # ...and no reset email came
    create_tried: bool = False     # credentials mode 'create' was used this run
    account_exists: bool = False   # a Create Account click said the account already exists
    consent_page_key: str = ""
    resolved: dict = field(default_factory=dict)   # norm(question) -> resolver outcome this run
    control_failures: dict = field(default_factory=dict)   # ref -> failed choose / set_date calls this run
    readback_open: dict = field(default_factory=dict)      # page -> read-back mismatches still showing when the run left it
    verdict: dict | None = None    # the judge's verdict from the submit check (verify.verify)
    judged: bool = False           # the judge ran this run (a held draft is not judged twice)
    auto_submitted: bool = False   # the verified auto-submit clicked Submit
    shadow_only: bool = False      # run-once --shadow: never auto-submit

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


# "No, I do not require sponsorship" is a No option: "no" is a whole word here, whatever punctuation follows (Harvey, 2026-10-18)
NEGATED = re.compile(r"^(not\b|no\b|none\b|i do not|i don['\u2019]t|i am not|i['\u2019]m not|unwilling|unable|never|decline|prefer not)", re.I)
AFFIRMATIVE = {"yes", "true", "checked", "on", "y"}
# an option label that says yes in its own words ("Yes, I am authorized", "I agree", "I am a U.S. citizen")
AFFIRM_LABEL = re.compile(r"^(yes\b|i (agree|consent|accept|acknowledge|certify|confirm|understand)\b|i am (authori[sz]ed|eligible|a )|i have\b|i will\b|agree\b|accept\b)", re.I)


# Yes/no questions about where Surya works or worked, industries, affiliations and relationships.
# Surya, 2026-10-15: "answer is almost always no (that's default if it stalls)".
def _plain_wordings(answer) -> list[str]:
    """The ';'-separated wordings of a stored answer, without structured entries: background.certifications holds
    "{'name': 'AWS Certified Cloud Practitioner', ...}", and as the head of background.certifications_variants that
    text was typed into Axiom's and Northrop's Workday certification search, became a free-text chip and was
    reported "set" (2026-10-19)."""
    out = []
    for w in str(answer if answer is not None else "").split(";"):
        w = w.strip()
        if w and not w.startswith(("{", "[")) and w not in out:
            out.append(w)
    return out


# A stored answer that is a rule for pickers ("Select every location offered...", "All offered locations",
# "San Francisco if only one is allowed"), never text for a box.
RULE_TEXT = re.compile(r"^\s*(select|pick|choose|tick|check|add|always|leave)\b|\bif only one\b|"
                       r"^\s*(all|every|any) (offered|available|listed)( \w+)?\s*$", re.I)


from ..adapters import LOCATION_KEY, POSTING_LOCATIONS, posting_locations  # noqa: E402  (Northrop Grumman, 2026-10-19)


def concrete_entries(answer) -> list[str]:
    """The entries of a ';' chain that are values, without picker rules."""
    return [w.strip() for w in str(answer if answer is not None else "").split(";") if w.strip() and not RULE_TEXT.search(w.strip())]


def text_from_chain(entries: list[str], label: str) -> str:
    """What a text box gets from an ordered chain: its first entry, or the first N distinct places when the box asks
    for "up to three" ("Dallas" and "Dallas, TX" are one place)."""
    m = re.search(r"\bup to (two|three|four|five|[2-5])\b|\b(two|three|four|five|[2-5]) (locations|offices|cities|choices|preferences)\b",
                  label or "", re.I)
    words = {"two": 2, "three": 3, "four": 4, "five": 5}
    n = 1
    if m:
        w = (m.group(1) or m.group(2)).lower()
        n = words.get(w) or int(w)
    picked, seen = [], set()
    for e in entries:
        k = answers.norm(e.split(",")[0])
        if k in seen:
            continue
        seen.add(k)
        picked.append(e)
        if len(picked) >= n:
            break
    return ", ".join(picked)


def workday_mail_quiet(hours: float = 8.0) -> bool:
    """Workday's account email stopped reaching his inbox on 2026-10-19 at 06:32 CDT, and every reset then waited its
    full three minutes for nothing. Three or more missed Workday resets and no opened one in the last `hours`: the
    waits drop to 45 s (the mail, when it comes, lands within a minute)."""
    con = db.connect()
    since = db.now() - int(hours * 3600)
    miss = con.execute("SELECT COUNT(*) FROM events WHERE ts > ? AND kind='creds:reset_not_received' AND detail LIKE '%myworkday%'",
                       (since,)).fetchone()[0]
    opened = con.execute("SELECT COUNT(*) FROM events WHERE ts > ? AND kind='creds:reset_link_opened' AND detail LIKE '%myworkday%'",
                         (since,)).fetchone()[0]
    return miss >= 3 and opened == 0


# A Workday account view's title left on an application form page (see t_snapshot).
STALE_ACCOUNT_TITLE = re.compile(r"^\s*(forgot password|reset password|sign in|create account)\b", re.I)
FORM_STEP_HEADING = re.compile(r"^(my information|my experience|application questions|voluntary disclosures|self identify)\b", re.I)


# A work-entry field: its value is Surya's stored entry (experience.work.N.*), never a résumé or generated literal.
WORK_ENTRY_FIELD = re.compile(r"^\s*(current |most recent |previous )?(company( name)?|employer( name)?|organi[sz]ation( name)?|"
                              r"job title|position title)\s*[*:]?\s*$", re.I)

AFFILIATION = re.compile(
    r"(employed (by|with|at)\b|\bwork(ed|ing)? (for|at)\b|\baffiliat|relationships? (with|to)\b|\brelatives?\b|family member|"
    # 'related to' only about a person: TIAA's "information related to your political contributions" took the default No
    r"\b(are|is|be) (you|anyone|any(one)? \w+) related to\b|\brelated to (anyone|someone|an? (current |former )?(employee|officer|director|board member|member|person|individual))|"
    r"household member|immediate family|\bspouse\b|board member|\bofficers?\b|\bdirectors?\b|government official|public official|"
    r"conflict of interest|financial interest|\bown (stock|shares)|\bvendors?\b|\bsuppliers?\b|\bcustomers?\b|\bclients?\b|"
    r"\bcompetitors?\b|subsidiar|parent company|former employee|current employee|independent contractor|consultant (for|to)|"
    r"\bindustry\b|close personal relation|domestic partner|significant other|\bkin\b|know anyone|acquaint|royal family|"
    r"political (party|office)|candidate for (political )?office|government (entity|employee|official)|member of your (family|household)|"
    # Northrop Grumman (2026-10-19): "performed services for Northrop Grumman as a contractor/contingent worker, or been
    # issued a Northrop Grumman security badge" went to Surya
    r"perform(ed)? services (for|to)|provided services (for|to)|contingent worker|security badge|"
    # GM Financial (2026-10-19): "Did you participate in the General Motors 2023 Voluntary Separation Package?" is a former-
    # employee question
    r"voluntary separation|separation (package|program|agreement)|severance (package|program|agreement)|early retirement)", re.I)
# Never a default No, even when they mention relatives, a spouse or customers (Penn State, 2026-10-18: "spouse or relatives
# who are foreign nationals; foreign travel; counseling; police records" was answered No by the affiliation rule):
# background-investigation, legal, financial and medical history, amounts of experience, and statements of willingness.
NOT_AFFILIATION = re.compile(
    r"(foreign (national|citizen|government|travel|contact|passport|activit|propert|business|financ|influence|preference)|"
    r"dual citizen|citizenship|passport|clearance|background (check|investigation)|"
    r"\bpolice\b|\barrest|\bconvict|criminal|felony|misdemeanor|\boffen[cs]e|\bcourt\b|\bcharged\b|probation|"
    r"illegal (drug|use)|drug use|\bsubstance|mental[- ]health|counsel+ing|psychiatr|"
    r"bankrupt|delinquen|\bdebts?\b|garnish|false claims|\bliable\b|debar|excluded from participation|\bsanctions?\b|"
    r"export control|\bitar\b|"
    r"how (much|many|long)|number of|\byears? of\b|\bmonths? of\b|"
    r"^\W*(i am|i'm|are you) (willing|able)\b|willing and able|\bwilling to\b)", re.I)
_YES = re.compile(r"^(yes|y|true)\b", re.I)
SELECT_ALL = {"all", "all offered locations", "all offered", "all locations", "all of the above", "select all", "every location", "any"}
_NO = re.compile(r"^(no|n|false|none)\b", re.I)
# A "No" in the portal's wording: what a default-No answer picks when the plain word is not offered.
_NEGATIVE_OPTION = re.compile(r"^(no\b|n\b|false\b|none\b|not to my knowledge|not that i('m| am)? aware|not applicable|n/?a\b|"
                              r"i (do not|don't) (know|have|believe)|i have (never|no)\b|i am not\b|neither\b)", re.I)
# Questions about people / employers the profile may name: a hit on one of them sends the question to the model.
_PROFILE_STOP = {"the", "and", "for", "with", "university", "department", "group", "inc", "llc", "corp", "company", "team", "club"}
FREE_RESPONSE = re.compile(
    r"^(why\b|describe|tell (us|me)|explain|share\b|what (interests|excites|motivates|makes|draws|attracts)|how (would|did|have|do) you|"
    r"please (describe|explain|tell|share|provide a (brief|short))|give (us )?an example|walk (us|me) through|what is (a|one|the) "
    r"(project|accomplishment|achievement|challenge)|what are you (most )?(proud|passionate|looking)|in (\d+|one|two|three) "
    r"(words|sentences)|cover letter|anything else)", re.I)


SOCIAL_SIGNIN = re.compile(r"(sign in|sign up|log ?in|continue|apply|register)\s+(with|using|via)\s+(google|linkedin|apple|microsoft|facebook|indeed)", re.I)
# What a portal says when the universal password is not the one this tenant holds (Workday, Oracle, iCIMS wordings).
SIGN_IN_FAILED = re.compile(
    r"(wrong email address or password|account might be locked|incorrect (email|username|password)|invalid (email|username|password|credentials|login)|"
    r"(email|username|password)( address)? (or password )?(is|was|you entered is) (incorrect|invalid|wrong)|could not (sign|log) you in|unable to (sign|log) in|"
    r"(sign|log)[- ]?in failed|authentication failed|password (does not|doesn't) match)", re.I)
ACCOUNT_EXISTS = re.compile(r"(already (exists|have an account|registered|in use)|an account with this email|email (address )?is already)", re.I)
RESET_STEPS = ("sign-in with the universal password failed on this tenant: do the password reset. Click 'Forgot your password?' (or 'Reset password'), "
               "fill the email box from answer_key identity.email, click the send button, call open_password_reset_link, snapshot, call credentials "
               "mode 'set_password' with the new-password ref (confirm box via confirm_password_ref, email_ref ''), click the save button, then "
               "credentials mode 'login_universal' and click Sign In.")
# No reset email came (Ryan, Assurant, Nasdaq, 2026-10-18): Workday only mails a reset for an account that exists, so
# the tenant most likely has none for this email. PASSWORD_BIT is the sign-up password too (Surya).
CREATE_STEPS = ("no reset email came, so this tenant most likely has no account for this email: create one. Go back to the sign-in page, "
                "click 'Create Account' (or 'Sign Up'), call credentials mode 'create' (email_ref = the email box, password_ref = the "
                "password box, confirm_password_ref = the verify-password box), tick the terms box if there is one, click Create Account, "
                "then call open_verification_link if the portal asks you to verify the email. Park credentials_unknown only if the "
                "portal says the account already exists.")


def is_affiliation_question(question: str, options: list[str] | None) -> bool:
    """A yes/no question in the employer/industry/affiliation family (options empty, or Yes/No-like)."""
    if not AFFILIATION.search(question or "") or NOT_AFFILIATION.search(question or ""):
        return False
    if re.search(r"(if (yes|so)|please (explain|describe|list|provide)|who\b|which\b|name of|names of)", question, re.I) and not options:
        return False
    if not options and FREE_RESPONSE.search(question or ""):
        return False          # "Describe your relationship with the team" is prose, not a yes/no
    opts = [o.strip() for o in (options or []) if o and o.strip() and not re.match(r"^(select|choose|please select)", o.strip(), re.I)]
    if not opts:
        return True
    return len(opts) <= 4 and any(_YES.match(o) for o in opts) and any(_NEGATIVE_OPTION.match(o) for o in opts)


_MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]


def date_option_covers(value: str, label: str) -> bool:
    """A grouped date option covers a stored date: 'April/May/June' or 'Apr - Jun' for 'May 2028' (month),
    '2028' or '2027-2028' for the year. Anything that is not a date never matches this way (Ashby, 2026-10-18)."""
    from ..browser.fill import parse_date
    parts = parse_date(value or "")
    if not parts.get("month") and not parts.get("year"):
        return False
    lab = (label or "").lower()
    tokens = [t.strip() for t in re.split(r"[/,;|]|\bor\b|\band\b|\s[-–—]\s", lab) if t.strip()]
    if parts.get("month"):
        name = _MONTH_NAMES[int(parts["month"]) - 1]
        hits = [t for t in tokens if re.fullmatch(rf"{name[:3]}[a-z]*(\s+\d{{4}})?", t)]
        if hits:
            yr = parts.get("year")
            return not yr or not re.search(r"\d{4}", lab) or yr in lab
        # a range of month names: "Apr - Jun"
        m = re.search(r"\b([a-z]{3,9})\s*[-–—to]+\s*([a-z]{3,9})\b", lab)
        if m:
            def idx(s):
                return next((i for i, n in enumerate(_MONTH_NAMES) if n.startswith(s[:3])), None)
            a, b = idx(m.group(1)), idx(m.group(2))
            if a is not None and b is not None and a <= int(parts["month"]) - 1 <= b:
                return True
    if parts.get("year") and not parts.get("month"):
        return bool(re.search(rf"\b{parts['year']}\b", lab))
    if parts.get("year") and parts.get("month") and re.fullmatch(rf"\s*{parts['year']}\s*", lab):
        return True
    return False


def default_no_choice(question: str, options: list[str] | None) -> str | None:
    """The wording a default-No answer takes on this form: 'No' for a yes/no box, else the single negative
    option offered ('Not to my knowledge' preferred), None when the form offers no negative wording."""
    opts = [o.strip() for o in (options or []) if o and o.strip() and not re.match(r"^(select|choose|please select)", o.strip(), re.I)]
    if not opts:
        return "No"
    negs = [o for o in opts if _NEGATIVE_OPTION.match(o)]
    if not negs:
        return None
    return next((o for o in negs if "knowledge" in o.lower()), negs[0])


def consent_question_key(question: str) -> str | None:
    """Consent / acknowledgement / certification questions Surya answers yes to on every form (his rule,
    2026-10-15), mapped to the standing key that authorizes it. Marketing opt-ins map to the No key."""
    from ..adapters import COMPLIANCE_DISCLOSURE, CONSENT, MARKETING, PROCESSING, SMS, consent_key, future_roles_consent
    q = question or ""
    if COMPLIANCE_DISCLOSURE.search(q):
        return "consent.compliance_disclosure"
    if future_roles_consent(q):
        return "consent.keep_profile_future_roles"   # his questionnaire Yes (2026-10-18); newsletters stay No
    asks_consent = re.search(r"\b(do|will|would) you (agree|accept|acknowledge|consent|certify|authori[sz]e|understand|confirm)\b", q, re.I)
    if SMS.search(q) and (asks_consent or CONSENT.search(q) or re.search(r"\b(receive|opt in|opt-in)\b", q, re.I)):
        return "consent.sms"
    if not CONSENT.search(q) and not MARKETING.search(q) and not asks_consent:
        return None
    if MARKETING.search(q) and not SMS.search(q) and not PROCESSING.search(q):
        return "consent.marketing_email"
    return consent_key(q)


# "Yes, I understand the above information regarding security clearances." against "No, I do not understand. I have
# questions..." (Penn State's clearance notice, 2026-10-19): a choice between understanding a notice and not
# understanding it is an acknowledgement, which his standing consent answers.
ACK_OPTION = re.compile(r"^\s*(yes\s*[,.:;!-]?\s*)?i (understand|acknowledge|have read|have reviewed|am aware)\b", re.I)


def acknowledgement_key(question: str, options: list[str] | None) -> str | None:
    """The consent key for a question whose choices are 'I understand / I acknowledge / I have read' (and its
    negation); None for every other question. Affiliation questions and marketing opt-ins never qualify."""
    opts = [str(o or "") for o in options or []]
    yes = next((o for o in opts if ACK_OPTION.match(o)), None)
    if yes is None or is_affiliation_question(question, opts):
        return None
    return consent_question_key(f"{question} | {yes}")


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

    NEXT_LABELS = ("next", "continue", "save and continue", "save & continue", "save", "proceed")
    TOOL_TIMEOUT_S = 240.0
    # tools that legitimately wait on Surya or on an email: their own deadline plus a margin
    WAITING_TOOLS = {"await_answers": "answer_wait_s", "ask_user": "answer_wait_s", "open_verification_link": "code_wait_s",
                     "open_password_reset_link": "code_wait_s", "enter_email_code": "code_wait_s"}

    # submit may run the verification (one judge call) before its click
    SLOW_TOOLS = {"submit": 480.0}

    def tool_timeout_s(self, name: str) -> float:
        attr = self.WAITING_TOOLS.get(name)
        if name == "enter_email_code":
            # the mail wait, then (no email) Surya typing the code on the website or in Telegram
            from ..human.questions import CODE_MIN_WAIT_S
            return float(SETTINGS.budgets.code_wait_s) + max(float(SETTINGS.budgets.answer_wait_s), CODE_MIN_WAIT_S) + 180.0
        if attr:
            return float(getattr(SETTINGS.budgets, attr, 900)) + 120.0
        return self.SLOW_TOOLS.get(name, self.TOOL_TIMEOUT_S)

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
            try:
                # No tool may hang the run: a page blocked by a dialog or a stuck network wait once held the single
                # worker for 12+ minutes (Persona AI, 2026-10-18). Waiting tools keep their own longer limits.
                waits = getattr(self.ctx.budget, "waiting", None) if name in self.WAITING_TOOLS else None
                with (waits() if waits else contextlib.nullcontext()):
                    out, img = await asyncio.wait_for(fn(**args), timeout=self.tool_timeout_s(name))
            except asyncio.TimeoutError:
                self.ctx.consecutive_failures += 1
                db.event(self.ctx.job_id, f"tool:{name}:timeout", {"args": args, "timeout_s": self.tool_timeout_s(name)})
                return json.dumps({"error": f"the {name} call did not finish within {int(self.tool_timeout_s(name))} s and was stopped. "
                                            "Take a snapshot; if the page is stuck, reload it with tabs action 'reload' and continue."}), None
            ok = not (isinstance(out, dict) and out.get("error"))
            self.ctx.consecutive_failures = 0 if ok else self.ctx.consecutive_failures + 1
            db.event(self.ctx.job_id, f"tool:{name}", {"args": args, "out": out}, latency_ms=int((time.monotonic() - t0) * 1000))
            return json.dumps(out, default=str), img
        except (Parked, BudgetExceeded):
            raise                          # a park or a spent budget ends the run; neither is a tool bug
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
        # After the password reset, Workday can show the application form while the page is still the "Forgot
        # Password" (or Sign In / Create Account) view underneath: picks made there do not stick (C&S Wholesale and
        # Itron's How Did You Hear stayed blank, 2026-10-19; a fresh load fixed C&S). Reload such a page once; the
        # session and the saved draft survive a reload.
        if STALE_ACCOUNT_TITLE.search(snap.title or "") and any(FORM_STEP_HEADING.search(h) for h in snap.headings) \
                and self.ctx.control_failures.get("stale_title:reload", 0) < 1:
            self.ctx.control_failures["stale_title:reload"] = 1
            try:
                await self.ctx.ex.page.reload(wait_until="domcontentloaded")
                await self.ctx.ex.settle()
                await asyncio.sleep(2.0)
                db.event(self.ctx.job_id, "page:stale_title_reload", {"title": (snap.title or "")[:80], "headings": snap.headings[:3]})
                snap = await snapmod.take(self.ctx.ex, with_text=with_text)
            except Exception as e:  # noqa: BLE001 - best effort
                db.event(self.ctx.job_id, "page:stale_title_reload_error", {"error": f"{type(e).__name__}: {e}"[:200]})
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
            # A radio / checkbox option is logged as "question / option", so the log says WHICH option was ticked
            # ('Were you previously employed at ABA?' = checked could be Yes or No; the judge read it as Yes, 2026-10-18).
            if item and item.get("type") in ("radio", "checkbox") and item.get("group"):
                opt = str(item.get("label") or "").strip()
                lab = str(f.get("label") or item.get("group") or "").strip()
                if opt and answers.norm(opt) not in answers.norm(lab):
                    f = {**f, "label": f"{lab[:120]} / {opt[:60]}"}
            if (f.get("source") or "").lower() == "computer":
                # The ref tools failed and the model picked the option with the computer tool: the pick stands only
                # when the page shows the stored answer of the key it came from. It used to be recorded 'as_is' as
                # Surya's own choice (Aerospace Corporation's enrollment status went to "Part-time Status", 2026-10-19).
                results.append(await self._keep_computer_pick(f, item))
                continue
            if (f.get("source") or "").lower() == "as_is" and not self._surya_kept_as_is():
                results.append({"ref": f.get("ref"), "status": "failed",
                                "error": "'as_is' is only for fields Surya filled himself and said so. For an option you picked with the "
                                         "computer tool, record it with source 'computer' and the answer_key it came from (value null)."})
                continue
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
                    first = answers.norm(str(a.answer).split(";")[0])
                    # "All offered locations" / "Select every location offered" presses every location toggle (American
                    # Express' Oracle offices are toggle buttons, 2026-10-19)
                    agree = (lab == ans) or (ans in ("yes", "true") and lab in ("yes", "true")) or (ans == "no" and lab in ("no", "false")) \
                        or (ans in ("yes", "true") and not NEGATED.match(item.get("label") or "") and lab not in ("no", "false")) \
                        or (ans == "no" and bool(NEGATED.match(item.get("label") or ""))) \
                        or ((first in SELECT_ALL or bool(re.match(r"^(select|pick|choose|tick|check) (every|all)\b", first)))
                            and not NEGATED.match(item.get("label") or "") and lab not in ("no", "false"))
                    parts = [x.strip() for x in str(a.answer).split(";") if x.strip()]
                    if not agree and len(parts) > 1:
                        # A ranked chain ("LinkedIn; Job Board; Agency / Search Firm; Other") presses the toggle of the
                        # first entry the group offers: Vertiv's Oracle pills read 'LinkedIn/Job Portal' (2026-10-19).
                        group = [o for o in (snap.items if snap else []) if o.get("tag") == "toggle"
                                 and (o.get("group") or "") == (item.get("group") or "")] or [item]
                        offered = [{"text": str(o.get("label") or ""), "_i": n} for n, o in enumerate(group)]
                        for part in parts:
                            if answers.norm(part) in SELECT_ALL:
                                continue
                            hit = fill.best_option(offered, [part])
                            if hit is not None:
                                chosen = group[hit["_i"]]
                                if chosen is item or chosen.get("ref") == item.get("ref"):
                                    agree = True
                                else:
                                    item = chosen
                                    f = {**f, "ref": chosen["ref"]}
                                    agree = True
                                break
                    if not agree:
                        # The stored answer names the OTHER half of a Yes/No pair: press that one (Primer's
                        # sponsorship toggles were left with neither pressed, 2026-10-18).
                        sibling = next((o for o in (snap.items if snap else []) if o.get("tag") == "toggle" and o is not item
                                        and (o.get("group") or "") == (item.get("group") or "") and (
                                            (ans in ("yes", "true") and answers.norm(o.get("label") or "") in ("yes", "true"))
                                            or (ans == "no" and answers.norm(o.get("label") or "") in ("no", "false"))
                                            or answers.norm(o.get("label") or "") == ans)), None)
                        if sibling is None:
                            results.append({"ref": f["ref"], "status": "failed", "error": f"stored answer for {f['answer_key']} is {a.answer!r} but this toggle reads {item.get('label')!r}; press the matching option instead"})
                            continue
                        item = sibling
                        f = {**f, "ref": sibling["ref"]}
                    f = {**f, "value": "true", "_force_true": True}
                    if item.get("group") and answers.norm(item.get("group") or "") != answers.norm(item.get("label") or ""):
                        f["label"] = f"{item.get('group')[:80]} / {item.get('label') or ''}"
            if is_choice and not f.get("answer_key") and (f.get("source") or "").lower() == "generated":
                results.append({"ref": f["ref"], "status": "failed", "error": "choices (radio/checkbox/select) need an answer_key: call lookup_answer, or ask_user if none fits"})
                continue
            if f.get("answer_key") == "identity.phone" and re.search(r"\bext(ension)?\b", f"{f.get('label') or ''} {(item or {}).get('label') or ''}", re.I):
                results.append({"ref": f["ref"], "status": "failed", "error": "a phone EXTENSION box stays empty: Surya has no extension "
                                "(his number goes in the Phone box only)"})
                continue
            if not f.get("answer_key") and (f.get("source") or "").lower() in ("resume", "generated") and (
                    WORK_ENTRY_FIELD.match(str(f.get("label") or "")) or WORK_ENTRY_FIELD.match(str((item or {}).get("label") or ""))):
                # T-Mobile (2026-10-19): asked by a mistaken judge note to "fix" work entry 5's company, the model typed
                # "Aggie Data Science Club" (source 'resume') over Surya's stored "Southwest Airlines", and Workday kept
                # only that entry on Save. Work entries come from his stored experience.work.N.* keys alone.
                results.append({"ref": f["ref"], "status": "failed", "error": (
                    "a work-entry field (company, employer, job title) holds Surya's stored entry: use the "
                    "experience.work.N.company / experience.work.N.title answer_key, or fill_sections for a Work Experience "
                    "section. A résumé or generated literal never replaces his entries; if a check calls the stored "
                    "entry wrong, leave it as it is.")})
                continue
            val, src, err = self._resolve_value(f.get("answer_key"), f.get("value"), f.get("source"), f.get("label", ""))
            if err:
                results.append({"ref": f["ref"], "status": "failed", "error": err})
                continue
            if f.get("_force_true"):
                val = "true"
            # A text box that asks for the posting's own locations gets every one of them (he is open to all), whatever
            # location key was named (Northrop Grumman, 2026-10-19).
            posting_locs: list[str] = []
            if item and item.get("tag") in ("input", "textarea", "richtext") and item.get("type") not in ("radio", "checkbox") \
                    and LOCATION_KEY.search(str(f.get("answer_key") or "")) \
                    and POSTING_LOCATIONS.search(f"{f.get('label') or ''} {item.get('label') or ''}"):
                posting_locs = posting_locations(self.ctx.job)
                if posting_locs:
                    val = "; ".join(posting_locs)
            # An ordered preference list ("LinkedIn; Job Board; Agency") is for pickers. A text box
            # takes the first choice only, never the whole chain.
            if f.get("answer_key") and item and item.get("tag") in ("input", "textarea", "richtext") \
                    and item.get("type") not in ("radio", "checkbox") and not posting_locs:
                a = answers.get(f["answer_key"], self.ctx.job.get("company", ""), self.ctx.portal)
                if a is not None and a.answer_type in ("choice", "multi_choice", "choice_chain"):
                    # A picker rule is not text: Ryan's "list up to three office locations" box got "Select every location
                    # offered. If only one can be chosen, pick San Francisco." and "All offered locations" (2026-10-19).
                    conc = concrete_entries(val)
                    if not conc and RULE_TEXT.search(str(val)):
                        results.append({"ref": f["ref"], "status": "failed", "error": (
                            f"{f['answer_key']} is a rule for pickers ({str(val)[:90]!r}), not text to type. For a text box "
                            "type the places themselves: availability.location_ranking gives his order (Dallas, Houston, "
                            "Austin first) for a location box.")})
                        continue
                    if a.answer_type == "choice_chain" or (";" in str(val) and len(conc) < len([w for w in str(val).split(";") if w.strip()])):
                        val = text_from_chain(conc or [str(val).split(";")[0].strip()],
                                              str(item.get("label") or f.get("label") or ""))
            # An ordered preference chain ("decline; ...; Asian") picks ONE option per question: the first entry the
            # question offers. Ticking every option that matched some entry ticked both "I Do Not Wish to Answer" and
            # "Asian" on HCSC's race checkboxes (2026-10-19).
            if is_choice and item and item.get("type") in ("radio", "checkbox") and f.get("answer_key") and ";" in str(val):
                a_chain = answers.get(f["answer_key"], self.ctx.job.get("company", ""), self.ctx.portal)
                parts0 = [x.strip() for x in str(val).split(";") if x.strip()]
                if item.get("type") == "radio":
                    # one choice only: "All offered locations" cannot be picked, so the ranking that follows decides
                    parts0 = [x for x in parts0 if answers.norm(x) not in SELECT_ALL] or parts0
                if a_chain is not None and a_chain.answer_type == "choice_chain" and answers.norm(parts0[0]) not in SELECT_ALL:
                    chosen = await self._chain_pick(f["ref"], parts0)
                    if chosen is not None and chosen.get("ref") != f["ref"]:
                        results.append({"ref": f["ref"], "status": "failed",
                                        "error": f"the stored answer is an ordered preference: in this question it picks "
                                                 f"'{str(chosen.get('label') or '')[:60]}' [{chosen.get('ref')}]; tick only that option"})
                        continue
            # Checkbox/radio with a text answer: check it when the option's own label matches the answer
            # ("Available May 2027" with answer "May 2027"); refuse when it plainly does not.
            label_matched = False
            if is_choice and item and item.get("type") in ("radio", "checkbox") and str(val).strip().lower() not in AFFIRMATIVE | {"false", "no", "off", "unchecked"}:
                lab = answers.norm(item.get("label") or ""); nv = answers.norm(str(val))
                parts = [answers.norm(p) for p in str(val).split(";") if answers.norm(p)]
                # "Spring 2028 (May 2028)" names the option "Spring 2028": a stored note in brackets is not part of the
                # option's wording (Sierra, 2026-10-18)
                parts += [s for s in (answers.norm(re.sub(r"\s*\([^)]*\)", "", p)) for p in str(val).split(";")) if s and s not in parts]
                if nv and (nv == lab or (len(nv) >= 4 and nv in lab) or any(p == lab or (len(p) >= 4 and p in lab) for p in parts)
                           or date_option_covers(str(val), item.get("label") or "")):
                    val = "true"
                    # The stored text names this option ("Not Specified"), negated or not: only when the label
                    # starts with it, so "willing" never ticks "Not willing to relocate".
                    label_matched = any(lab.startswith(x) for x in [nv, *parts] if x)
                elif item.get("type") == "checkbox" and parts and (parts[0] in SELECT_ALL or re.match(r"^(select|pick|choose|tick|check) (every|all)\b", parts[0]))                         and not NEGATED.match((item.get("label") or "").strip()):
                    # ("Select every location offered (multi-select)..." in his bank answer: American Express, 2026-10-19)
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
            # A yes/no answer on a RADIO option ticks only the option that says the same thing. A radio cannot be
            # unticked, so "No" sent to the "Yes" option used to tick Yes (American Family, 2026-10-18).
            if is_choice and item and item.get("type") == "radio" and not label_matched:
                v = str(val).strip().lower()
                opt_label = (item.get("label") or "").strip()
                says_no = answers.norm(opt_label) in ("no", "false", "n") or bool(NEGATED.match(opt_label))
                says_yes = answers.norm(opt_label) in ("yes", "true", "y") or bool(AFFIRM_LABEL.match(opt_label))
                if v in ("no", "false", "off", "unchecked", "n"):
                    if says_no:
                        val = "true"
                        label_matched = True
                    else:
                        results.append({"ref": f["ref"], "status": "failed",
                                        "error": f"the stored answer is No but this option reads '{opt_label[:50]}'; tick the No option of this "
                                                 "question instead (a radio cannot be unticked)"})
                        continue
                elif v in AFFIRMATIVE and says_no:
                    results.append({"ref": f["ref"], "status": "failed",
                                    "error": f"the stored answer is Yes but this option reads '{opt_label[:50]}'; tick the Yes option instead"})
                    continue
                elif v in AFFIRMATIVE and not says_yes and answers.norm(opt_label) and f.get("answer_key"):
                    a_ = answers.get(f["answer_key"], self.ctx.job.get("company", ""), self.ctx.portal)
                    if a_ is not None and answers.norm(str(a_.answer)) in ("yes", "no"):
                        # a plain Yes/No answer only ticks a Yes-like option, never "Maybe" / "Prefer not to say"
                        results.append({"ref": f["ref"], "status": "failed",
                                        "error": f"the stored answer is Yes but this option reads '{opt_label[:50]}'; tick the Yes option instead"})
                        continue
            # A checkbox group answered No ticks its own "No" / "None of the above" box; the other boxes (the firms it
            # lists) stay unticked. American Century's 'Grant Thornton / FORVIS / Deloitte and Touche / No' boxes kept
            # 'No' unticked and the run was held (2026-10-19).
            if is_choice and item and item.get("type") == "checkbox" and not label_matched \
                    and str(val).strip().lower() in ("no", "false", "n", "none"):
                opt_label = (item.get("label") or "").strip()
                if answers.norm(opt_label) in ("no", "none", "none of the above", "n a", "not applicable") \
                        or re.match(r"^(no|none)\b", opt_label, re.I):
                    val = "true"
                    label_matched = True
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
            dec_label = f.get("label", "")
            opt = str((item or {}).get("label") or "").strip()
            if item and (item.get("type") or "").lower() in ("radio", "checkbox") and opt and                     answers.norm(opt) not in answers.norm(dec_label):
                # a radio / checkbox decision names its option ("Were you previously employed at ABA? / No"): logged as
                # the bare question with 'checked', ABA's judge read it as Yes (2026-10-19)
                dec_label = f"{dec_label[:120]} / {opt[:60]}"
            plan.append({"ref": f["ref"], "value": val, "_src": src, "_label": dec_label, "_text": not is_choice,
                         "_key": f.get("answer_key"), "_source": f.get("source")})
        if plan:
            res = await fill.fill_batch(self.ctx.ex, [{"ref": p["ref"], "value": p["value"]} for p in plan])
            by = {r.ref: r for r in res}
            for p in plan:
                r = by.get(p["ref"])
                if r is not None and not r.ok and str(r.error or "").startswith("picker:"):
                    # an Oracle list picker keeps only a value picked from its list: choose types with real keys and picks
                    # the match (American Express' ZIP / City / State / County took typed text and stayed empty, 2026-10-19)
                    out, _ = await self.t_choose(p["ref"], p.get("_key"), [] if p.get("_key") else [str(p["value"])],
                                                 p["_label"], p.get("_source"))
                    d = dict(out) if isinstance(out, dict) else {"status": "failed", "error": "choose gave no result"}
                    d.setdefault("ref", p["ref"])
                    d["via"] = "choose"
                    results.append(d)
                    continue
                d = r.as_dict() if r else {"ref": p["ref"], "status": "failed", "error": "no result"}
                results.append(d)
                if r and r.ok:
                    records.record_decision(self.ctx.job_id, self.ctx.page_no, p["_label"], r.readback, p["_src"], p["ref"])
        self.ctx.budget.step()
        set_n = sum(1 for r in results if r.get("status") == "set")
        # A field set again after a page's read-back complained is the fix for that complaint: drop it, so a mismatch
        # recorded under an older page number does not outlive its fix (HCSC, 2026-10-19). The next Next click re-checks.
        fixed = {r.get("ref") for r in results if r.get("status") in ("set", "kept") and r.get("ref")}
        for page in list(self.ctx.readback_open):
            left = [m for m in self.ctx.readback_open[page] if not any(f"[{ref}]" in m for ref in fixed)]
            if left:
                self.ctx.readback_open[page] = left
            else:
                self.ctx.readback_open.pop(page, None)
        kept_n = sum(1 for r in results if r.get("status") == "kept")
        # "kept" (the page already shows his answer, now recorded) is not a failure: "0 set, 4 failed" for four kept
        # answers sent T-Mobile's model back over fields that were right (2026-10-19).
        return {"summary": f"{set_n} set, {kept_n} kept, {len(results) - set_n - kept_n} failed" if kept_n else
                f"{set_n} set, {len(results) - set_n} failed", "results": results}, None

    SENSITIVE_KEYS = re.compile(r"^(self_id\.|compliance\.(date_of_birth|criminal|disciplined|debarment)|bank\.(gender|race|ethnic|veteran|disab|transgender|sexual))", re.I)

    async def t_choose(self, ref: str, answer_key: str | None, options: list[str], label: str, source: str | None):
        out, img = await self._t_choose(ref, answer_key, options, label, source)
        if isinstance(out, dict) and (out.get("status") in ("set", "kept") or str(out.get("summary", "")).startswith("1 set")):
            # a picker set again fixes the read-back complaint recorded for it when its page was left; fill_batch already
            # drops those, choose did not, and American Express' verify kept failing on pickers that held the right values
            for page in list(self.ctx.readback_open):
                left = [m for m in self.ctx.readback_open[page] if f"[{ref}]" not in m]
                if left:
                    self.ctx.readback_open[page] = left
                else:
                    self.ctx.readback_open.pop(page, None)
        if answer_key and answer_key.startswith("background.certification") and self.ctx.portal == "workday" \
                and isinstance(out, dict) and out.get("status") == "failed":
            try:
                note = await self._drop_unoffered_certification(ref)
            except Exception as e:  # noqa: BLE001 - best effort; the model still has the failure
                db.event(self.ctx.job_id, "cert:drop_error", {"error": f"{type(e).__name__}: {e}"[:200]})
                note = None
            if note:
                out = {**out, "note": note}
        return out, img

    def _cert_unavailable(self) -> bool:
        """This tenant's certification list was found without his AWS Certified Cloud Practitioner (this run or an
        earlier one on the same host)."""
        if self.ctx.control_failures.get("cert:unavailable"):
            return True
        host = re.sub(r"^https?://([^/]+).*$", r"\1", self.ctx.ex.url or "").lower()
        if not host:
            return False
        row = db.connect().execute("SELECT 1 FROM events WHERE kind='cert:unavailable' AND detail LIKE ? LIMIT 1",
                                   (f'%"{host}"%',)).fetchone()
        return bool(row)

    async def _drop_unoffered_certification(self, ref: str) -> str | None:
        """Axiom Space and Northrop Grumman list other AWS exams but not his Cloud Practitioner; the model kept adding a
        certification block, failing the pick and parking over the empty required box (2026-10-19). The harness deletes
        that block itself, remembers the host, and refuses another Add Certification there."""
        from ..adapters.workday_sections import WorkdaySections
        ws = WorkdaySections(self.ctx.ex, self.ctx.job, self.ctx.page_no)
        info = await ws.scan()
        certs = [e for e in info["entries"] if re.search(r"certif|licen", e["kind"], re.I)]
        blk = next((e for e in certs if any(f.get("ref") == ref for f in e["fields"].values())), None)
        if blk is None and len(certs) == 1:
            blk = certs[0]
        if blk is None:
            return None
        deleted = await ws._delete_entry(blk)
        host = re.sub(r"^https?://([^/]+).*$", r"\1", self.ctx.ex.url or "").lower()
        db.event(self.ctx.job_id, "cert:unavailable", {"host": host, "block": f'{blk["kind"]}-{blk["idx"]}', "deleted": deleted})
        self.ctx.control_failures["cert:unavailable"] = 1
        if not deleted:
            return ("this tenant's Certification list does not offer his AWS Certified Cloud Practitioner. Press this "
                    "certification block's Delete button (certifications are optional) and do not add one again: a Review "
                    "showing 'Certifications: No Response' is correct. Never pick another certification in its place.")
        return ("this tenant's Certification list does not offer his AWS Certified Cloud Practitioner, so the harness deleted "
                "that certification block (certifications are optional). Do not add one again: a Review showing "
                "'Certifications: No Response' is correct. Continue with Save and Continue.")

    async def _t_choose(self, ref: str, answer_key: str | None, options: list[str], label: str, source: str | None):
        if not answer_key and re.search(r"\bcertif|\blicen[cs]e", label or "", re.I) \
                and not re.search(r"date|issued by|issuer|number|expir|state", label or "", re.I):
            # TIAA (2026-10-19): with no option for his AWS Certified Cloud Practitioner, the model picked "Cloud Guild:
            # AWS Cloud Practitioner - TIAA" (an internal course) with source 'resume'.
            return {"error": "a certification is picked only with answer_key background.certifications_variants (his AWS "
                             "Certified Cloud Practitioner); another credential is never chosen in its place. If the list "
                             "does not offer it, delete the certification block (certifications are optional)."}, None
        if answer_key and self.SENSITIVE_KEYS.match(answer_key) and options:
            # Surya's self-identification answers are his stored chain and nothing else; a model-supplied
            # fallback ("Asian (...)" next to "Decline to self-identify") is never tried.
            db.event(self.ctx.job_id, "answer:self_id_guard", {"key": answer_key, "dropped": [str(o)[:60] for o in options][:6], "label": label[:80]})
            options = []
        snap_item = (self.ctx.last_snapshot.find(ref) if self.ctx.last_snapshot else None) or {}
        if answer_key and (snap_item.get("type") or "").lower() in ("checkbox", "radio"):
            # A box of a radio / checkbox group is no picker (Epic Games' Gender Identity boxes, 2026-10-19: choose
            # reported "the picker for this field did not open"): tick it as fill_batch does, with the answer's
            # chain, the self-ID guard and the decision format the read-back expects.
            return await self.t_fill_batch([{"ref": ref, "answer_key": answer_key, "value": None, "source": source, "label": label}])
        wants = list(options or [])
        stored_wants: list[str] = []
        src = "adapter"
        if answer_key:
            val, src, err = self._resolve_value(answer_key, None, None, label)
            if err:
                return {"error": err}, None
            wants = _plain_wordings(val) + wants
            # Portal spellings of the same answer ("Texas A & M University" on Workday) live in <key>_variants.
            variants = answers.get(answer_key + "_variants", self.ctx.job.get("company", ""), self.ctx.portal)
            if variants and variants.has_value:
                wants += [w for w in _plain_wordings(variants.answer) if w not in wants]
            stored_wants = _plain_wordings(val)
            if variants and variants.has_value:
                stored_wants += _plain_wordings(variants.answer)
            # "June" and "2026" for a stored "06/01/2026" on a month or year picker are his answer in the picker's words:
            # Coinbase's employment months were recorded as judgment and the fact check failed them (2026-10-19)
            stored_wants += [w for w in fill.date_wants(stored_wants)
                             + (fill.month_numbers(stored_wants) if re.search(r"month", label or "", re.I) else [])
                             if w not in stored_wants]
            if answer_key.endswith("_variants"):
                # The variants are the portal spellings tried AFTER the answer itself: picked by the variants key,
                # Epic Games' school search got only "Texas A & M University"-style wordings, all unknown to it,
                # and never "Texas A&M University" (2026-10-19).
                base = answers.get(answer_key[: -len("_variants")], self.ctx.job.get("company", ""), self.ctx.portal)
                if base and base.has_value:
                    head = _plain_wordings(base.answer)
                    wants = head + [w for w in wants if w not in head]
                    stored_wants = head + stored_wants
        elif not wants:
            return {"error": "give options or an answer_key"}, None
        model_opts = [o for o in (options or []) if o]
        his_wants = [w for w in wants if w not in model_opts or w in stored_wants]
        if answer_key and not stored_wants:
            # An empty stored answer settles nothing: Epic Games' Unreal Engine question got "Yes" from the model's own
            # options under experience.anything_else_default (empty), recorded as sourced (2026-10-19).
            return {"error": f"{answer_key} has no stored value for this question: lookup_answer it for the right key, or ask_user"}, None
        r = None
        if answer_key and his_wants and model_opts:
            # His answer first, alone: with the model's options mixed in, a stale wrong value that matches one of them
            # ("Yes" left by an earlier wrong key) was kept although the stored answer is "No" (Epic Games, 2026-10-19).
            first = await fill.choose(self.ctx.ex, ref, his_wants)
            r = first if first.ok else None
        if r is None:
            r = await fill.choose(self.ctx.ex, ref, wants)
        self.ctx.budget.step()
        if r.ok:
            shown = str(r.readback)
            # Matched only by an option the model supplied, not by the stored answer or its variants: the model's own
            # mapping ("Not Applicable" recorded under work_authorization.us_citizen = Yes on Nike, 2026-10-19). It is
            # recorded as judgment so the fact check before an automatic submit looks at it.
            mine = bool(answer_key) and bool(his_wants) and fill.best_option([{"text": shown}], his_wants) is None \
                and not any(answers.norm(w) and answers.norm(w) in answers.norm(shown) for w in his_wants) \
                and not fill._chip_matches(shown, his_wants)
            rec_src = f"judgment:{answer_key}" if mine else src
            records.record_decision(self.ctx.job_id, self.ctx.page_no, label, shown, rec_src, r.ref or ref)
            self.ctx.control_failures.pop(ref, None)
            out = r.as_dict()
            first = next((w for w in his_wants if w), "") or next((w for w in wants if w), "")
            if mine:
                out["note"] = (f"{shown[:60]!r} came from your options, not from the stored answer {first[:60]!r}: it must mean the same, "
                               "and the check before submit will verify it. If it does not, pick the option that says the stored answer.")
            elif first and answers.norm(shown) != answers.norm(first):
                out["note"] = (f"{shown[:60]!r} is this portal's wording for {first[:40]!r} (one of the stored variants): "
                               "it is the correct, sourced value. Do not re-set it, do not ask, do not park over it.")
            return out, None
        return self._with_fallback_hint(ref, r.as_dict()), None

    def _surya_kept_as_is(self) -> bool:
        """Did Surya answer one of this job's questions with "I already filled it out" (AS_IS)?"""
        from ..human.questions import AS_IS
        row = db.connect().execute("SELECT 1 FROM pending_questions WHERE job_id=? AND answer=? LIMIT 1",
                                   (self.ctx.job_id, AS_IS)).fetchone()
        return row is not None

    async def _keep_computer_pick(self, f: dict, item: dict | None) -> dict:
        ref = f.get("ref", "")
        key = f.get("answer_key")
        a = answers.get(key, self.ctx.job.get("company", ""), self.ctx.portal) if key else None
        if a is None or not a.has_value:
            return {"ref": ref, "status": "failed", "error": "a pick made with the computer tool is recorded with the answer_key it came "
                                                             "from (lookup_answer first); with no stored answer, ask_user"}
        try:
            info = await self.ctx.ex.read_ref(ref)
        except Exception:
            info = {}
        typ = str(info.get("type") or "").lower()
        if typ in ("checkbox", "radio"):
            if info.get("value") != "checked":
                return {"ref": ref, "status": "failed", "error": "that option is not ticked on the page"}
            shown = str((item or {}).get("label") or info.get("label") or "")
        else:
            shown = str(info.get("value") or info.get("shown") or "")
        wants = [w.strip() for w in str(a.answer).split(";") if w.strip()]
        variants = answers.get(key + "_variants", self.ctx.job.get("company", ""), self.ctx.portal)
        if variants and variants.has_value:
            wants += [w.strip() for w in str(variants.answer).split(";") if w.strip()]
        ok = bool(shown) and (fill.best_option([{"text": shown}], wants) is not None
                              or any(answers.norm(w) and answers.norm(w) in answers.norm(shown) for w in wants))
        if not ok:
            return {"ref": ref, "status": "failed",
                    "error": f"the page shows {shown[:60]!r} but the stored answer for {key} is {str(a.answer)[:80]!r}: pick the option that "
                             "says that, or ask_user when none does"}
        records.record_decision(self.ctx.job_id, self.ctx.page_no, f.get("label") or (item or {}).get("label", ""), shown[:300],
                                f"standing:{key}", ref)
        return {"ref": ref, "status": "kept", "readback": shown[:120]}

    def _with_fallback_hint(self, ref: str, out: dict) -> dict:
        """Surya (2026-10-18): when the ref tools cannot work a control, go to screenshot mode and click it by
        hand. After the second failure on the same ref the model is told to switch to the computer tool."""
        n = self.ctx.control_failures.get(ref, 0) + 1
        self.ctx.control_failures[ref] = n
        if n >= 2:
            out["fallback"] = ("the ref tools failed on this control twice: switch to the `computer` tool now. Take a screenshot, "
                              "click the control, type or scroll inside its list, click the option that matches the stored answer, then "
                              "`snapshot` and record it with fill_batch: the same answer_key, value null, source 'computer' (kept only "
                              "when the page shows that answer).")
        return out

    async def t_click(self, ref: str, label: str):
        snap = self.ctx.last_snapshot
        item = snap.find(ref) if snap else None
        if item and item.get("submitish"):
            return {"error": "that is a submit control; call submit instead"}, None
        if re.search(r"\badd\b", label or "", re.I) and re.search(r"certif|licen", label or "", re.I) and self._cert_unavailable():
            return {"error": "not clicked: this tenant's Certification list does not offer his AWS Certified Cloud Practitioner "
                             "(checked on this portal already). Certifications are optional: leave the section empty; a Review "
                             "showing 'Certifications: No Response' is correct."}, None
        if item and SOCIAL_SIGNIN.search(item.get("label") or label or ""):
            return {"error": "never sign in through Google, LinkedIn, Apple, Microsoft or Facebook. Click 'Sign in with email' "
                             "(or the email / password form), then call `credentials`."}, None
        if item and (item.get("type") in ("radio", "checkbox") or item.get("tag") in ("select", "dropdown", "toggle")):
            return {"error": "that is a choice control; set it with fill_batch (radio/checkbox/toggle, value from an answer_key) or choose (dropdown) so the decision has a source"}, None
        if re.search(r"forgot (your )?password|reset (your )?password", label, re.I) and not self.ctx.reset_needed \
                and not re.search(r"^reset password$", label.strip(), re.I) and self.ctx.control_failures.get("creds:forgot_bounce", 0) < 1:
            # A reset only after a sign-in with the universal password failed in this run (Wawa, 2026-10-19).
            self.ctx.control_failures["creds:forgot_bounce"] = 1
            return {"error": "not clicked: sign in first. On the Sign In form call credentials login_universal and click Sign In; "
                             "the password reset is for when that sign-in fails."}, None
        if label.lower().strip() in self.NEXT_LABELS:
            # Read-back BEFORE leaving the page, while its controls still exist. A mismatch between a decision and what
            # its control shows stops the first click so it can be fixed (it never blocks twice); whatever still differs
            # when the page is left is kept, and the verified auto-submit refuses while any remains.
            from ..worker import readback_after_page
            snap_now = await snapmod.take(self.ctx.ex)
            self.ctx.last_snapshot = snap_now
            mism = await readback_after_page(self.ctx, snap_now, self.ctx.job_id, self.ctx.page_no)
            if mism and self.ctx.control_failures.get(f"readback:{self.ctx.page_no}", 0) < 1:
                self.ctx.control_failures[f"readback:{self.ctx.page_no}"] = 1
                return {"error": "not clicked: before leaving this page, these fields show something other than what was decided: "
                                 + "; ".join(mism[:6]) + ". Re-set them from their answer_keys (if the page shows the same answer in its own "
                                 "wording, it is fine), then click again."}, None
            if mism:
                self.ctx.readback_open[self.ctx.page_no] = mism
            else:
                self.ctx.readback_open.pop(self.ctx.page_no, None)
        info = await self.ctx.ex.click_ref(ref)
        await self.ctx.ex.settle(quiet_ms=400)
        note = await self.ctx.ex.follow_new_tab()
        self.ctx.budget.step()
        new_url = self.ctx.ex.url
        if info.get("tag") in ("button", "a") and label.lower().strip() in self.NEXT_LABELS:
            self.ctx.page_no += 1
        # The snapshot's label first: the click script's own label falls back to the nearest heading, and T-Mobile's
        # "Save and Continue" came back as the enrollment question above it, so the model went back to redo a page
        # that had saved (2026-10-19).
        out = {"clicked": (item.get("label") if item else None) or info.get("label") or label, "url": new_url}
        if note:
            out["note"] = note
        # After a Sign In / Create Account click the harness reads the outcome itself: a failed sign-in with the
        # universal password means this tenant holds an older one, and the reset flow is the next step.
        clicked = (info.get("label") or label or "").strip()
        if self.ctx.creds_mode in ("login", "login_universal", "create") and re.search(r"sign ?in|log ?in|create account|sign up|continue|next|submit", clicked, re.I):
            await self.ctx.ex.settle(quiet_ms=600)
            try:
                text = await self.ctx.ex.page_text(30000)
            except Exception:
                text = ""
            m = SIGN_IN_FAILED.search(text) if self.ctx.creds_mode != "create" else None
            if m:
                self.ctx.reset_needed = True
                self.ctx.control_failures["creds:login_failed"] = self.ctx.control_failures.get("creds:login_failed", 0) + 1
                db.event(self.ctx.job_id, "creds:sign_in_failed", {"host": self.ctx.portal_host, "quote": m.group(0)[:80]})
                out["sign_in"] = "failed"
                out["next"] = RESET_STEPS
            elif self.ctx.creds_mode == "create" and ACCOUNT_EXISTS.search(text):
                self.ctx.reset_needed = True
                self.ctx.account_exists = True
                db.event(self.ctx.job_id, "creds:account_exists", {"host": self.ctx.portal_host})
                out["account"] = "exists"
                if self.ctx.reset_not_received:
                    out["next"] = ("the account exists, the universal password fails and no reset email came: park credentials_unknown "
                                   "(Surya resets it by hand)")
                else:
                    out["next"] = "the account already exists on this tenant: go to Sign In, call credentials login_universal and click Sign In; if that fails, " + RESET_STEPS
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

    async def t_fill_sections(self):
        from ..adapters.workday_sections import WorkdaySections
        if self.ctx.portal == "greenhouse":
            from ..adapters.greenhouse_sections import GreenhouseSections, is_employment_page
            snap = await snapmod.take(self.ctx.ex)
            if not is_employment_page(snap):
                return {"added": [], "updated": [], "present": [], "failed": [], "skipped": ["no Employment section (Company name / Title blocks) on this page"],
                        "note": "this form has no employment section; nothing to add"}, None
            rep = await GreenhouseSections(self.ctx.ex, self.ctx.job, self.ctx.page_no).run()
        else:
            rep = await WorkdaySections(self.ctx.ex, self.ctx.job, self.ctx.page_no).run()
        db.event(self.ctx.job_id, "adapter:sections", {**rep.summary(), "by": "model"})
        out = rep.summary()
        if not rep.added and not rep.updated and not rep.failed:
            out["note"] = "no Work Experience / Websites section found on this page (or nothing stored applies); take a snapshot"
        else:
            out["note"] = "take a snapshot: the refs changed. Finish only the failed items, then Education and the résumé."
        return out, None

    async def t_go_to_step(self, step: str):
        import asyncio
        want = step.strip().lower()
        visits = self.ctx.control_failures.get(f"step:{want}", 0) + 1
        self.ctx.control_failures[f"step:{want}"] = visits
        last = " ".join(str(r) for r in ((self.ctx.gate_result or {}).get("reasons") or [])).lower()
        # the gate itself sends the run back ("the Review page does not show work entry 2 ...: call go_to_step('My
        # Experience')"): two more trips are allowed then (Aptiv, 2026-10-19, stalled between the two instructions)
        asked = f"go_to_step('{step.strip().lower()}')" in last or (want.startswith("my experience") and "does not show work entry" in last)
        if visits > 2 and not (asked and visits <= 4):
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

    async def _chain_pick(self, ref: str, parts: list[str]) -> dict | None:
        """The option an ordered chain picks in the radio / checkbox group holding `ref`: the first chain entry (its
        bracket note dropped as a second try) that some option's label matches. The group comes from the snapshot, else
        from the page (the options sharing the nearest container). None when no group or no entry matches."""
        snap = self.ctx.last_snapshot
        group = next((g for g in (getattr(snap, "groups", None) or []) if any(o.get("ref") == ref for o in g.get("options", []))), None)
        options = group.get("options", []) if group else []
        if not options:
            try:
                sib = await self.ctx.ex.eval(js.JS_CHOICE_SIBLINGS, ref)
            except Exception:
                sib = []
            options = sib if isinstance(sib, list) else []
        if len(options) < 2:
            return None
        opts = [(o, answers.norm(o.get("label") or "")) for o in options]
        for part in parts:
            for cand in (answers.norm(part), answers.norm(re.sub(r"\s*\([^)]*\)", "", part))):
                if not cand:
                    continue
                for o, lab in opts:
                    if cand == lab or (len(cand) >= 4 and cand in lab):
                        return o
        return None

    async def t_upload_resume(self, ref: str, document: str):
        if document == "transcript":
            item = self.ctx.last_snapshot.find(ref) if self.ctx.last_snapshot else None
            lab = str((item or {}).get("label") or "")
            if re.search(r"\b(graduate|grad school|master'?s|ph\.?d|doctoral)\b", lab, re.I) and not re.search(r"undergrad", lab, re.I):
                # Verkada (2026-10-18): the transcript went into the Graduate Transcript slot and the undergraduate one stayed empty
                return {"error": f"'{lab[:60]}' is a graduate-school slot; Surya is an undergraduate (BS, graduating May 2028). Upload the "
                                 "transcript into the Undergraduate Transcript slot and leave this one empty."}, None
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
        choice = default_no_choice(question, options)
        hits = profile.mentions(question)
        used_model = False
        if choice is not None and not hits:
            # Deterministic (Surya's standing rule, 2026-10-18): no employer or person from his profile is named,
            # so the answer is No in whatever wording the form offers, without a model call.
            ans, why = choice, "default No (standing rule: the question names nobody from the profile)"
        elif self.ctx.classifier is not None and facts:
            used_model = True
            ans, why = await self.ctx.classifier.yes_no_from_profile(self.ctx.job_id, question, options, facts)
            if ans == "No" and choice:
                ans = choice
        else:
            ans, why = choice or "No", "default No"
        key = answers.key_from_question(question)
        is_no = bool(_NEGATIVE_OPTION.match(ans))
        stored = ans if ans == "No" or not is_no else f"{ans}; No"
        answers.save_answer(key, stored, answer_type="choice_chain" if ";" in stored else "choice",
                            source="default_no" if is_no else "profile", note=f"auto from profile: {why[:150]}", aliases=[question])
        db.event(self.ctx.job_id, "answer:auto_affiliation", {"key": key, "answer": ans, "why": why[:200], "model": used_model, "mentions": hits[:4]})
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
        ack = acknowledgement_key(question, opts)
        if ack:
            a = answers.get(ack, company, portal)
            if a is not None and a.has_value and ack != "consent.marketing_email":
                pres = self._present(a, via="consent_rule")
                pres.update({"note": "an acknowledgement of a notice: Surya acknowledges on every form (his standing rule); "
                                     "pick the choice that says he understands", "choices": opts})
                db.event(self.ctx.job_id, "answer:acknowledgement", {"key": ack, "question": question[:160]})
                result = (pres, {"meaning": "acknowledgement of a notice", "choices": opts})
                self.ctx.resolved[nq] = result
                return result
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

    # A government-employment question that counts public / state universities as government entities: his stored No
    # was written for federal government, and he worked in a Texas A&M research group (Booz Allen's question 6,
    # 2026-10-19). Only his own answer to that wording settles it.
    GOV_PUBLIC = re.compile(r"(public|state)[- ](funded |run )?(universit|college|institution|school)|universit\w* (owned|funded|operated|run) by (a |the )?state", re.I)
    GOV_TOPIC = re.compile(r"government|public entit|political", re.I)

    async def t_lookup_answer(self, question: str, options: list[str], meaning: str | None = None, ref: str | None = None):
        text = " ".join([question or "", meaning or "", " ".join(options or [])])
        if self.GOV_PUBLIC.search(text) and self.GOV_TOPIC.search(text):
            k = answers.key_from_question(question)
            mine = answers.get(k, self.ctx.job.get("company", ""), self.ctx.portal)
            if mine is not None and mine.has_value:
                return self._present(mine), None
            return {"miss": True, "key": k, "hint": "this form counts public / state universities as government entities, and Surya worked in a "
                    "Texas A&M research group (a public university). His stored No to government employment covers federal government only: "
                    "call ask_user with this question and its choices."}, None
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
        if mode in ("login", "login_universal") and self.ctx.reset_needed and not self.ctx.reset_link_tried \
                and self.ctx.control_failures.get("creds:login_failed", 0) >= 1:
            # The same password would fail again (Assurant, GE Vernova, 2026-10-18): the reset flow first.
            return {"error": RESET_STEPS}, None
        # The form has to match the mode (Wawa, 2026-10-19): Workday's "Apply Manually" opens Create Account; sign-in
        # credentials typed there, then a click on its "Sign In" link, left an empty sign-in form and the run went
        # straight to a password reset without ever signing in.
        snap = self.ctx.last_snapshot
        pw_boxes = sum(1 for i in (getattr(snap, "items", None) or []) if (i.get("type") or "").lower() == "password")
        if mode in ("login", "login_universal") and pw_boxes >= 2:
            return {"error": "this is the Create Account form (two password boxes). To sign in, click its 'Sign In' link first, take a "
                             "snapshot, then call credentials login_universal on the sign-in form and click its Sign In button."}, None
        if mode == "create" and pw_boxes == 1:
            return {"error": "this is the Sign In form (one password box). Click 'Create Account' first, take a snapshot, then call "
                             "credentials mode 'create' with the password and verify-password boxes."}, None
        if mode == "create":
            self.ctx.create_tried = True
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
        if status.startswith("filled"):
            self.ctx.creds_mode = mode
            if mode == "set_password":
                self.ctx.reset_needed = False
        return {"status": status, "email": email}, None

    async def t_enter_email_code(self, code_refs: list[str]):
        if self.ctx.mail is None:
            return {"error": "mail helper not configured (Gmail API)"}, None
        import asyncio
        with db.tx() as con:
            con.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (states.AWAITING_CODE, db.now(), self.ctx.job_id))
        # A run that resumes on a code page did not request the code itself: Coinbase's arrived 7 minutes before the
        # rerun asked for it and a 3-minute look-back missed it (2026-10-19).
        # A code Surya already typed in the website's code box for this application comes first.
        given = BROKER.recent_code(self.ctx.job_id)
        if given:
            key, code = given
            ok = await fill.type_code_into(self.ctx.ex, code_refs, code)
            BROKER.mark_code_used(self.ctx.job_id, key)
            with db.tx() as con:
                con.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (states.FILLING, db.now(), self.ctx.job_id))
            self.ctx.budget.step()
            for r in (code_refs or [None]):
                records.record_decision(self.ctx.job_id, self.ctx.page_no, "emailed verification code", "(set)",
                                        "user:verification_code", r or "")
            db.event(self.ctx.job_id, "code:entered_from_surya", {"boxes": len(code_refs or []), "provided": True})
            return {"status": "entered" if ok else "entered_unverified", "via": "the code Surya typed on the website",
                    "next": "take a snapshot, then call submit once more"}, None
        after_ts = self.ctx.code_requested_at or (db.now() - 900)
        fetch = self.ctx.mail.fetch_code
        try:
            import inspect
            kw = {"company": self.ctx.job.get("company", "")} if "company" in inspect.signature(fetch).parameters else {}
        except (TypeError, ValueError):
            kw = {}
        handle = await asyncio.to_thread(fetch, self.ctx.portal, self.ctx.portal_host, after_ts, **kw)
        if handle is None and not self.ctx.code_requested_at:
            # the code may predate this run by more than 15 minutes (a held job retried later): one wider look; an expired
            # code is refused by the page, and the model then asks the portal for a new one
            handle = await asyncio.to_thread(fetch, self.ctx.portal, self.ctx.portal_host, db.now() - 3600, **kw)
        if handle is None:
            # No email found: Surya types the code on the website or in Telegram (his request, 2026-10-19).
            code = await self._ask_surya_for_code()
            if code:
                ok = await fill.type_code_into(self.ctx.ex, code_refs, code)
                with db.tx() as con:
                    con.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (states.FILLING, db.now(), self.ctx.job_id))
                self.ctx.budget.step()
                for r in (code_refs or [None]):
                    records.record_decision(self.ctx.job_id, self.ctx.page_no, "emailed verification code", "(set)",
                                            "user:verification_code", r or "")
                db.event(self.ctx.job_id, "code:entered_from_surya", {"boxes": len(code_refs or [])})
                return {"status": "entered" if ok else "entered_unverified", "via": "Surya typed it on the website / Telegram",
                        "next": "take a snapshot, then call submit once more"}, None
            with db.tx() as con:
                con.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (states.FILLING, db.now(), self.ctx.job_id))
            return {"status": "not_received", "hint": "Surya did not send it either. Request a new code once if the page offers it, "
                                                      "then call again; otherwise park with 'code_not_received'"}, None
        ex = self.ctx.ex
        loop = asyncio.get_event_loop()
        ok_box = {"ok": False}

        def _type(code: str) -> None:
            ok_box["ok"] = asyncio.run_coroutine_threadsafe(fill.type_code_into(ex, code_refs, code), loop).result(60)

        status = await asyncio.to_thread(handle.enter, _type)
        with db.tx() as con:
            con.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (states.FILLING, db.now(), self.ctx.job_id))
        self.ctx.budget.step()
        # One decision per code box, on its ref: Greenhouse's eight "Security code" boxes each held a character the gate
        # saw as a value with no sourced decision, so Coinbase's submit stayed blocked after the code was in (2026-10-19).
        for r in (code_refs or [None]):
            records.record_decision(self.ctx.job_id, self.ctx.page_no, "emailed verification code", "(set)",
                                    "policy:may_enter_emailed_verification_code", r or "")
        return {"status": status if ok_box["ok"] else "entered_unverified", "message_id": getattr(handle, "message_id", None)}, None

    async def _ask_surya_for_code(self) -> str | None:
        """Post a code question (website card + Telegram) and wait for the code he types. The code is used once: the
        question row keeps '(used)' afterwards and nothing is saved as an answer."""
        from ..human.questions import BROKER as broker
        key = f"code.{self.ctx.job_id}.{db.now()}"
        company = self.ctx.job.get("company") or "the portal"
        question = (f"Verification code needed: {company} ({self.ctx.portal_host}) emailed you a code, and the agent could not "
                    "find that email. Type the code here and the agent enters it into the form.")
        await broker.ask(self.ctx.job_id, [{"key": key, "question": question, "field_type": "code", "options": []}])
        db.event(self.ctx.job_id, "code:asked_surya", {"key": key, "host": self.ctx.portal_host})
        try:
            got = await broker.await_answers(self.ctx.job_id, [key])
        except Parked:
            db.event(self.ctx.job_id, "code:not_given", {"key": key})
            return None
        code = (got or {}).get(key)
        broker.mark_code_used(self.ctx.job_id, key)
        return code or None

    async def t_open_password_reset_link(self):
        if self.ctx.mail is None:
            return {"error": "mail helper not configured (Gmail API)"}, None
        import asyncio
        self.ctx.reset_link_tried = True
        after_ts = db.now() - 600
        fetch = getattr(self.ctx.mail, "fetch_password_reset_link", None)
        if fetch is None:
            return {"error": "the mail helper cannot fetch reset links"}, None
        quiet = self.ctx.portal == "workday" and workday_mail_quiet()
        res = await asyncio.to_thread(fetch, self.ctx.portal, self.ctx.portal_host, after_ts, **({"timeout_s": 45} if quiet else {}))
        status = getattr(res, "status", "not_received")
        if quiet and status != "reset_link":
            db.event(self.ctx.job_id, "creds:mail_quiet", {"host": self.ctx.portal_host, "waited_s": 45})
        if status == "reset_link" and getattr(res, "url", None):
            await self.ctx.ex.goto(res.url)
            self.ctx.budget.step()
            db.event(self.ctx.job_id, "creds:reset_link_opened", {"host": self.ctx.portal_host})
            return {"status": "opened", "url": self.ctx.ex.url,
                    "next": "snapshot, then credentials mode 'set_password' with the new-password and confirm refs (email_ref ''), click the save button, then sign in with login_universal"}, None
        if status == "host_mismatch":
            raise Parked("host_mismatch: the reset link pointed at another host", "hold")
        self.ctx.reset_not_received = True
        db.event(self.ctx.job_id, "creds:reset_not_received", {"host": self.ctx.portal_host, "status": status})
        if self.ctx.create_tried or self.ctx.account_exists:
            return {"status": status, "hint": "no reset email came and the account exists: park credentials_unknown"}, None
        return {"status": status, "next": CREATE_STEPS}, None

    async def t_open_verification_link(self, timeout_s: float | None = None):
        if self.ctx.mail is None:
            return {"error": "mail helper not configured (Gmail API)"}, None
        import asyncio
        # Look back a day: an account created in an earlier attempt is still waiting on its email, and only
        # links on THIS portal's host (from this tenant) are ever accepted.
        after_ts = db.now() - 86400
        self.ctx.verify_link_tried = True
        if timeout_s is None and self.ctx.portal == "workday" and workday_mail_quiet():
            timeout_s = 45
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
        if fill.autosaves(ctx.ex):
            await fill.ashby_resave(ctx.ex)   # an answer shown but never stored is missing on submit (Sierra, 2026-10-19)
        snap = await snapmod.take(ctx.ex)
        ctx.last_snapshot = snap
        if any(i.get("type") == "password" for i in snap.items):
            return {"error": "this page has a password field: it is a sign-in or account page, not the application. Use `credentials` "
                             "for the email and password, then `click` Sign In / Create Account, and fill the application that follows."}, None
        page_text = await ctx.ex.page_text(30000)
        gate = run_gate(job=ctx.job, snapshot=snap, page_text=page_text, expected_resume=ctx.resume_file,
                        free_text_answers=[f["answer"] for f in ctx.free_text], portal=ctx.portal, page_no=ctx.page_no)
        ctx.gate_result = gate.as_dict()
        db.event(ctx.job_id, f"gate:{gate.result}", {**gate.as_dict(), "cleared_live": bool(ctx.job.get("cleared_live"))})
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
            if not (SETTINGS.auto_submit_on() and not ctx.shadow_only):
                ctx.submitted = {"status": "shadow", "gate": gate.as_dict()}
                raise Parked("shadow_mode: gate passed; stopping before the final click", "hold")
            # Verified auto-submit (Surya 2026-10-18): submit only when every check agrees the information is correct;
            # otherwise hold it for his OK on Telegram (goat/verify.py).
            from ..verify import verify
            v = await verify(ctx, ctx.ex)
            if not v.ok and v.fixable and ctx.control_failures.get("verify:fix", 0) < 1:
                # One chance to fix what the check found (a blank required field, a wrong pick, an unsupported claim)
                # before Surya is asked (CoVar and Range, 2026-10-18).
                ctx.control_failures["verify:fix"] = 1
                db.event(ctx.job_id, "verify:fix_requested", {"fixes": v.fixes[:6]})
                with db.tx() as con:
                    con.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (states.FILLING, db.now(), ctx.job_id))
                return {"status": "blocked", "gate": "verification",
                        "fix_these_then_call_submit_again": v.fixes[:6],
                        "note": "the code gate passed, but the final check found these; fix them, then call submit once more"}, None
            if not v.ok:
                ctx.submitted = {"status": "review_needed", "gate": gate.as_dict()}
                raise Parked("review_needed: " + v.summary(), "hold")
            db.event(ctx.job_id, "auto_submit:verified", {"unconfirmed": v.unconfirmed, "judge": (v.verdict or {}).get("verdict")})
            ctx.auto_submitted = True
        res = await do_submit(ctx.ex, ctx.job_id, label=button_label or "")
        db.event(ctx.job_id, f"submit:{res.status}", res.as_dict())
        if res.status == "submitted":
            ctx.submitted = res.as_dict()
            return {"status": "submitted", "signal": res.signal, "note": "done; the worker records the ledger"}, None
        if res.status == "failed":
            code_why = [w for w in (res.why or []) if re.search(r"(security|verification|access|one[- ]time) code", str(w), re.I)]
            if code_why:
                # this click made the portal email a fresh code: enter_email_code waits for that one and never takes an
                # older code (Coinbase got its stale 09:31 code at 10:08 and the portal rejected it, 2026-10-19)
                ctx.code_requested_at = db.now() - 30
            if len(code_why) < len(res.why or []) or not res.why:
                ctx.budget.failed_submit()   # may raise BudgetExceeded; a click that only asks for the emailed code is a step, not a failure
            with db.tx() as con:
                con.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (states.FILLING, db.now(), ctx.job_id))
            return {"status": "failed", "why": res.why, "note": "do not click again; fix a named blocker then call submit once more, or park"}, None
        with db.tx() as con:
            con.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (states.FILLING, db.now(), ctx.job_id))
        return {"status": "blocked", "fix_these_then_call_submit_again": res.why}, None

    SATISFIED = re.compile(r"(work authorization|authorized to work|sponsorship|citizen|bachelor|gpa|18 years|relocat|onsite|on-site|hybrid|remote|2028|rising senior|eligible to obtain|clearance eligib|"
                           r"\bsatisf|40 hours|full[- ]time|hours (per|a) week|\d+ weeks|start date|begin|beginning|available from|"
                           r"(may|june) \d{1,2},? 2027|summer 2027|weekend|in[- ]person|commut|related (field|discipline|major|area|degree)|computer science|local to|during the internship|"
                           # class standing (Nissan, 2026-10-19: "students entering their Junior or Senior year"): he is a
                           # junior by credits and a rising senior for Summer 2027 (2027-28 is his final year)
                           r"\bjunior|\bsophomore|class standing|academic (standing|classification)|year in school|rising (junior|senior)|"
                           r"entering (their |his )?(junior|senior|third|fourth|final))", re.I)
    # What makes a ready_for_you park final: something only Surya can do or decide. Anything else ("resume application
    # fields", "complete the demographic questions") goes through the gate first (t_park).
    RFY_REASON = re.compile(r"captcha|human|\bbot\b|verif|challenge|prohibit|directly|the applicant|themsel|personally|in person|\bai\b|"
                            r"automat|essay|upload|\bfile\b|portfolio|transcript|document|\bssn\b|social security|consent|"
                            r"legal|eligib|clearance|sponsor|not (offered|available|listed|selectable)|no (selectable|matching) |"
                            r"cannot be (set|selected|answered)|picker .*(not|no)\b", re.I)
    RFY_GIVE_UP = re.compile(r"^(ready_for_you|needs_you):\s*(please\s+)?(complete|finish|fill|continue|resume|fix|enter|provide|answer|select|add)\b", re.I)
    # Surya 2026-10-18: a car requirement is fine (he arranges one); "local / within N miles" is fine when the posting is
    # paid; a straight hardware role is not.
    DISQUALIFIERS = re.compile(r"(master'?s|ph\.?d|doctoral|graduate program|active (secret|top secret|ts/sci|clearance)|returning intern|co-?op|fall 2027|spring 2028|quant|"
                               r"unpaid|hardware (engineer|design|role)|\b(fpga|asic|rtl|verilog|vhdl|pcb)\b|circuit design|analog design)", re.I)

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
        # a one-sided bound: "graduation on or after December 2027" / "December 2027 or later" sets only the low end,
        # "graduating by / before / no later than June 2028" only the high end (Motorola, 2026-10-19)
        if len(dates) == 1:
            if re.search(r"\b(on or after|after|or later|no earlier than|at the earliest|from)\b", posting, re.I):
                return lo > (2028, 5)
            if re.search(r"\b(by|before|on or before|no later than|or earlier|at the latest)\b", posting, re.I):
                return hi < (2028, 5)
        return lo > (2028, 5) or hi < (2028, 5)

    # A posting the portal no longer has (AtkinsRealis on Workday, 2026-10-18: "The page you are looking for doesn't exist"):
    # the job is closed, not a portal error the watchdog keeps retrying.
    POSTING_GONE = re.compile(r"(page you are looking for (doesn'?t|does not|no longer) exist|job (you are looking for |posting )?(is )?no longer "
                              r"(available|exists|open|active)|(position|job|requisition) has been (filled|closed|removed)|no longer accepting "
                              r"applications|this job (has expired|is closed)|job not found)", re.I)

    async def t_park(self, reason: str):
        # A portal failure page (Workday "Something went wrong ... Error Code") cannot be fixed on the form:
        # park it as a portal error at once; the watchdog retries it in a fresh tab later.
        from ..gate import portal_error
        try:
            page_now = await self.ctx.ex.page_text(30000)
        except Exception:
            page_now = ""
        gone = self.POSTING_GONE.search(page_now or "")
        if gone and not self.ctx.submitted:
            raise Parked(f"posting_closed: the portal says {gone.group(0)[:80]!r}", "dead")
        perr = portal_error(page_now) if page_now else None
        if perr and self.ctx.control_failures.get("portal:reload", 0) < 1:
            # One refresh first: Workday's "Something went wrong" page usually clears on reload (ICF, 2026-10-18).
            self.ctx.control_failures["portal:reload"] = 1
            try:
                await self.ctx.ex.page.reload(wait_until="domcontentloaded")
                await self.ctx.ex.settle()
                perr = portal_error(await self.ctx.ex.page_text(30000))
            except Exception:
                pass
            if not perr:
                db.event(self.ctx.job_id, "park:bounced", {"reason": reason[:200], "reloaded": True})
                return {"error": "not parked: the page was refreshed and the portal error is gone. Take a snapshot and continue "
                                 "(on Workday, if a step shows No Response on Review, go_to_step there and call fill_sections)."}, None
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
                                 "from the stored value is the portal's own spelling, which is correct. A Workday prompt nests options under "
                                 "categories: a chip showing the stored value is picked even when the top-level list does not show it (T-Mobile, "
                                 "2026-10-19). Move on to the rest of the page and submit."}, None
        if re.search(r"race|ethnicit|hispanic|latino|gender|veteran|disabilit|self[- ]identif", low) and self.ctx.park_attempts < 3:
            # Self-identification never parks: every wording has a stored chain, including his own answers for forms
            # with no decline option (Surya, 2026-10-18).
            self.ctx.park_attempts += 1
            db.event(self.ctx.job_id, "park:bounced", {"reason": reason[:200], "self_id": True})
            return {"error": "not parked: self-identification fields are settled by their self_id.* keys (choose with the key; the chain ends with "
                             "his own answers for forms without a decline option). Whatever the picker shows after that is correct. Move on: "
                             "finish the other sections (Work Experience via go_to_step('My Experience') if the Review shows No Response) and submit."}, None
        if low.startswith(("needs_you", "ready_for_you")) and re.search(r"relocat|\breside|residence|housing|commut|\blocal\b", low) \
                and not re.search(r"captcha|human|\bbot\b|unpaid|no pay|volunteer", low) \
                and self.ctx.control_failures.get("park:relocation", 0) < 2:
            # Living in the job's city, housing and relocation help are his to arrange: he relocates for a paid summer at
            # his own expense (AutoZone's "relocation assistance depends on family housing in Memphis", 2026-10-19).
            self.ctx.control_failures["park:relocation"] = self.ctx.control_failures.get("park:relocation", 0) + 1
            db.event(self.ctx.job_id, "park:bounced", {"reason": reason[:200], "relocation": True})
            return {"error": "not parked: residing in the job's city, housing and relocation (with or without assistance) are Surya's "
                             "to arrange: he relocates anywhere in the U.S. for the summer at his own expense. A relocation-assistance "
                             "question takes his stored relocation answers. Continue the application."}, None
        if low.startswith("credentials") and self.ctx.reset_not_received and not self.ctx.create_tried \
                and not self.ctx.account_exists and self.ctx.control_failures.get("creds:create_bounce", 0) < 1:
            # The reset email never came: the account most likely does not exist here. Create it before giving up.
            self.ctx.control_failures["creds:create_bounce"] = 1
            db.event(self.ctx.job_id, "park:bounced", {"reason": reason[:200], "credentials": "create not tried"})
            return {"error": "not parked: " + CREATE_STEPS}, None
        if low.startswith("credentials") and self.ctx.mail is not None and not self.ctx.reset_link_tried \
                and self.ctx.control_failures.get("creds:bounce", 0) < 2:
            # One application password everywhere: a credentials park is allowed only after the reset flow was
            # tried (whatever the vault's state). Wrong-password pages get the reset steps; others, the helper.
            self.ctx.control_failures["creds:bounce"] = self.ctx.control_failures.get("creds:bounce", 0) + 1
            try:
                text = await self.ctx.ex.page_text(30000)
            except Exception:
                text = ""
            db.event(self.ctx.job_id, "park:bounced", {"reason": reason[:200], "credentials": "reset not tried"})
            if SIGN_IN_FAILED.search(text) or self.ctx.reset_needed:
                return {"error": "not parked: " + RESET_STEPS}, None
            return {"error": "not parked: the password helper is configured. On a Sign In page call credentials login_universal and click Sign In; "
                             "on a Create Account page call credentials mode 'create' (it types the email, password and confirm box) and click Create Account. "
                             "If the page then reports a wrong password, do the reset flow (open_password_reset_link); park only after that was tried."}, None
        if (low.startswith("credentials") or "verify your" in low or "verification" in low) and not self.ctx.verify_link_tried \
                and self.ctx.mail is not None:
            # An account that exists but cannot sign in is usually waiting on its verification email (a short
            # lookback: the email, if any, was sent earlier; never the full code wait here).
            try:
                out, _ = await self.t_open_verification_link(timeout_s=20)
            except Exception:              # Parked, or Gmail unreachable (an SSLError broke a park, 2026-10-18): best effort only
                out = {}
            if out.get("status") == "verified":
                db.event(self.ctx.job_id, "park:bounced", {"reason": reason[:200], "verified": True})
                return {"error": "not parked: opened the account verification link from this portal's email. Sign in again "
                                 "(credentials mode 'login_universal', then click Sign In) and continue the application."}, None
        if low.startswith("needs_you") and re.search(r"work[- ]?(history|experience)|entr(y|ies)|website|skills?", low) \
                and self.ctx.portal == "workday" and self.ctx.control_failures.get("sections:bounce", 0) < 1:
            self.ctx.control_failures["sections:bounce"] = 1
            db.event(self.ctx.job_id, "park:bounced", {"reason": reason[:200], "sections": True})
            return {"error": "not parked: the harness adds the work-history entries and websites itself. Call fill_sections on the My Experience "
                             "page (go_to_step('My Experience') first if you are elsewhere), finish what it reports failed, then continue."}, None
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
        if self.ctx.park_attempts < 2 and not low.startswith("credentials") and (low.startswith("automation_prohibited") or "password" in low or "create account" in low or "sign in" in low):
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
        # season terms in a GRADUATION window ("expected graduation in Fall 2027, Spring/Summer 2028 or 2029") are no
        # co-op term: grad_window_excludes judges those (Arconic was held on a window that includes May 2028, 2026-10-19)
        non_grad = " ".join(part for part in re.split(r"[;.]", reason)
                            if not re.search(r"graduat|class of|degree (by|in|before)|completion date", part, re.I))
        if low.startswith("eligibility") and self.SATISFIED.search(reason) and not self.DISQUALIFIERS.search(non_grad) \
                and not self.grad_window_excludes(reason) and self.ctx.park_attempts < 2:
            self.ctx.park_attempts += 1
            db.event(self.ctx.job_id, "park:bounced", {"reason": reason[:200]})
            return {"error": "not parked: that requirement is SATISFIED by the standing facts (U.S. citizen, authorized, no sponsorship, "
                             "a junior by credits and a rising senior for Summer 2027 since 2027-28 is his final year ('Sophomore' is only "
                             "his current year on forms), "
                             "Bachelor's in progress, GPA 4.0, relocates anywhere, graduates May 2028). A posting stating it is not a restriction. "
                             "Continue: open the application (click Apply / Apply Manually) and fill it."}, None
        # "ready_for_you" stands for what only Surya can do (a CAPTCHA, a posting that wants the applicant himself, a
        # file goat does not have). Without such a reason it is checked like any eager park: Moderna's "resume
        # application fields" quit on My Information and Epic Games' "complete the demographic questions" quit on
        # questions his stored answers cover (2026-10-19).
        # A reason that only says what is left to do ("complete the CSX Oracle application fields, including the full home
        # address, work experience, education, remaining questions, and e-signature", 2026-10-19) is never final.
        # A CAPTCHA counts only when the page has one: Epic Games parked "complete the required CAPTCHA/human
        # verification if presented; required fields remain: phone, ..." on a form with no challenge (2026-10-19).
        snap0 = self.ctx.last_snapshot
        real_challenge = bool(snap0 is not None and (getattr(snap0, "challenge", None) or {}).get("challenge"))
        stoppers = [m.group(0) for m in self.RFY_REASON.finditer(reason)
                    if real_challenge or not re.search(r"captcha|human|\bbot\b|challenge|verif", m.group(0), re.I)]
        # four gate-checked bounces, as for any eager park: American Express spent two (one on a timed-out session) and its
        # third, "Oracle VERIFY requires a human click" on a page already showing the form, stood (2026-10-19)
        rfy_soft = low.startswith(("ready_for_you", "needs_you")) and self.ctx.park_attempts < 4 and (
            not stoppers or (bool(self.RFY_GIVE_UP.search(reason)) and not real_challenge))
        hard = reason.lower().startswith(("ready_for_you", "bot_check", "automation_prohibited", "eligibility", "answer_", "credentials", "code_not", "host_mismatch", "needs_you")) \
            and not rfy_soft
        # past the budget, a park is still checked twice when the page has a submit control: Northrop Grumman spent its
        # four parks on a portal error that had cleared, then parked from Review over an earlier attempt's certification
        # problem that was no longer on the page (2026-10-19)
        extra = not hard and self.ctx.park_attempts >= 4 and self.ctx.control_failures.get("park:past_budget", 0) < 2 \
            and bool(self.ctx.last_snapshot is not None and has_submit_control(self.ctx.last_snapshot))
        if extra:
            self.ctx.control_failures["park:past_budget"] = self.ctx.control_failures.get("park:past_budget", 0) + 1
        if not hard and (self.ctx.park_attempts < 4 or extra):
            # An eager park: run the gate first. If it passes, the right move is submit; if it blocks,
            # the model gets the concrete blockers instead of giving up. Four checks, not two: Epic Games spent both
            # within 30 s (one on the posting page before Apply opened the form) and its third park went through
            # with "requires an unavailable resume attachment" on a form whose blockers were all fillable (2026-10-19).
            self.ctx.park_attempts += 1
            snap = await snapmod.take(self.ctx.ex)
            self.ctx.last_snapshot = snap
            page_text = await self.ctx.ex.page_text(30000)
            gate = run_gate(job=self.ctx.job, snapshot=snap, page_text=page_text, expected_resume=self.ctx.resume_file,
                            free_text_answers=[f["answer"] for f in self.ctx.free_text], portal=self.ctx.portal, page_no=self.ctx.page_no)
            db.event(self.ctx.job_id, "park:checked", {"reason": reason[:200], "gate": gate.as_dict()})
            self.ctx.gate_result = gate.as_dict()
            if gate.passed:
                return {"error": "not parked: the code gate PASSES on this page. A problem an EARLIER attempt recorded is not on "
                                 "this page now: judge the page, not the history. Call submit now (the gate decides; the "
                                 "checks before submitting stop anything wrong)."}, None
            from ..gate import NOT_APPLICATION
            if extra:
                pass   # past the budget and the gate blocks: the park stands
            elif gate.result == "blocked" and low.startswith("portal_error") and gate.reasons and all(r.startswith(NOT_APPLICATION) for r in gate.reasons)                     and any("error or maintenance notice" in r for r in gate.reasons):
                pass   # the page shows the portal's own error or maintenance notice: the park stands
            elif gate.result == "blocked" and gate.reasons and all(r.startswith(NOT_APPLICATION) for r in gate.reasons):
                # a mid-form page (no submit control) is no portal error: Northrop Grumman parked on My Information after
                # Workday sent it back there with the last attempt's values (2026-10-19)
                return {"error": "not parked: this is a form page, not a portal error. Re-set its fields from their answer_keys "
                                 "(values the last attempt left are fine once sourced), then Save and Continue forward to Review "
                                 "and call submit."}, None
            elif gate.result == "blocked":
                hint = ""
                if any(re.search(r"resume|résumé|\bcv\b", r, re.I) for r in gate.reasons):
                    hint = " The résumé is on file: call upload_resume on the Resume/CV slot."
                return {"error": "not parked yet: the gate names concrete blockers. Fix them, then call submit; park only if one truly has no stored answer." + hint,
                        "blockers": gate.reasons}, None
        raise Parked(reason, state, blocked_on=pending if state == "hold" else None)
