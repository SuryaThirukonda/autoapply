"""Judge: one gpt-6-luna verdict per finished shadow draft (roadmap 2026-10-18 item 5).

After the gate passes in shadow mode the run stops before the click. The judge then reads the decision
log and the final screenshot and says whether every required field holds the intended value, with a
bucketed reason when not. The verdict is stored with the job (judgements table), logged as events and
shown by `goat show`, the dashboard and the Telegram finish message. It never raises and is never
called on a live submission."""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
import logging
import time

from . import db
from .config import SETTINGS, ModelNotAllowed, assert_allowed

log = logging.getLogger(__name__)

VERDICTS = ("pass", "fail", "unsure")
BUCKETS = ("none", "missing_required", "wrong_value", "wrong_option_wording", "unsourced_value", "date_not_committed",
           "file_not_attached", "consent_or_attestation", "layout_or_portal_error", "other")

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "bucket": {"type": "string", "enum": list(BUCKETS)},
        "notes": {"type": "string", "description": "one or two sentences: what is wrong, or 'all required fields hold the intended values'"},
        "fields": {"type": "array", "items": {"type": "string"}, "description": "labels of the fields that are wrong or missing"},
        "unsupported": {"type": "array", "items": {"type": "string"},
                        "description": "labels of UNCONFIRMED ANSWERS whose facts the applicant profile and the stated evidence do not "
                                       "support, each with a few words on why; empty when every one is supported or none are listed"},
    },
    "required": ["verdict", "bucket", "notes", "fields", "unsupported"],
    "additionalProperties": False,
}

def surya_answers(job_id: int | None) -> str:
    """The questions this job put to Surya that he answered, with his answers ('(none)' when there are none)."""
    if job_id is None:
        return "(none)"
    try:
        rows = db.connect().execute(
            "SELECT question, answer FROM pending_questions WHERE job_id=? AND answered_at IS NOT NULL AND answer IS NOT NULL "
            "AND answer != '' ORDER BY id", (int(job_id),)).fetchall()
    except Exception:  # noqa: BLE001 - the judge runs without it
        return "(none)"
    lines = [f"- Q: {str(r['question'] or '')[:300]}\n  A: {str(r['answer'] or '')[:300]}" for r in rows
             if not str(r["answer"] or "").startswith("__")]
    return "\n".join(lines) if lines else "(none)"


JUDGE_INSTRUCTIONS = (
    "You audit a finished draft of an online job application before it is submitted. You get the decision log (every "
    "field the agent set: label, value it read back, the source of that value), the text of the final page and a "
    "screenshot. Decide whether every required field holds the intended value: the value in the log, in the portal's "
    "own wording where the portal rewords options. His English is native: a language-proficiency value set from "
    "experience.english_proficiency is correct at the top level the form offers ('Native', 'Fluent', 'Expert', 'Advanced', "
    "or 'Proficient' on a scale that ends there); only a low level (Beginner, Basic, Elementary, Limited, Intermediate) is "
    "wrong (Ecolab, 2026-10-20). A field-of-study, degree or school value in the portal's own catalog "
    "wording is the intended value: 'Computer and Information Science', 'Computer Information Science', 'Computer and "
    "Information Sciences', 'Computer sciences' or 'Computer Science and Engineering' for Computer Science (Parsons, "
    "2026-10-24), 'Bachelor of Science' / 'BS' / 'Bachelors' / 'Bachelor's Level Degree' / "
    "'Bachelor's degree' / 'Bachelor Degree' for his BS, 'Texas A & M University' for Texas A&M (Dimensional, 2026-10-20; "
    "Micron, 2026-10-21; General Motors, 2026-10-24). "
    "A date the log sources to background.certification_* (06/26/2026, his AWS Certified Cloud Practitioner) sits in a "
    "certification / license entry: its 'Date Obtained', 'Issue Date' or 'Date Issued' is the certificate's date and is "
    "correct there; it is no citizenship date (Westinghouse's 'Date Obtained 1' under Professional Certifications & "
    "Licenses, 2026-10-25). "
    "A required 'career fair / event / university attended' list that offers no 'none', 'N/A' or 'did not attend' option "
    "takes 'Other' (with 'N/A', 'Not applicable' or 'None' as the event name where a name is asked: all say no event): he "
    "found the posting online, and Other is the only "
    "choice that names no event he did not attend (Gilead, 2026-10-24: Keck ... Yale, Other). "
    "Whether he previously interned, co-oped or worked AT THE COMPANY HE IS APPLYING TO is answered only by the roles his "
    "profile lists: a stored general answer such as bank.prior-internship ('Yes': his DeKalb internship) is about other "
    "employers and never supports a Yes to 'Have you previously completed an internship at <this company>?'; with no such "
    "role in the profile, No is the supported answer and Yes is wrong_value (UL Solutions went out with that Yes, "
    "2026-10-24). "
    "An LGBTQ+ / LGBTQIA+ community membership 'No' logged from self_id.lgbtq_member is his own answer, never unsourced: "
    "he is heterosexual and not transgender, and his stored orientation answer itself says 'I do not identify as a member "
    "of the LGBTQ+ community' (SeatGeek, Red Ventures, 2026-10-24). "
    "A how-heard 'Other' whose source says it was picked inside a category is that category's answer: General Motors' only "
    "'Other' sits inside 'Internet Job Board / Job Search', and the portal's chip shows the leaf alone (2026-10-24). "
    "A Title / Salutation of 'Mr' or 'Mr.' logged from identity.salutation is his stored answer (male, he/him), never an "
    "unsourced value (AutoZone, 2026-10-21; onsemi passed with the same). "
    "SURYA'S OWN ANSWERS lists what he answered himself to questions this application asked: those are his decisions for "
    "the fields they name, and a value that follows one is intended even where the profile says otherwise (AutoZone, "
    "2026-10-21: he told it to give his current Revio role today's end date where Oracle required one). "
    "His year in school is Sophomore on US forms; a portal that counts years to graduation words the same year "
    "'Penultimate academic year' / 'Penultimate year' / 'Second Year' / '2nd Year' / 'Year 2' (he graduates May 2028, so "
    "2026-27 is the second of his three years), and that wording logged from education.class_standing or its variants "
    "is the intended value, never a wrong one (LSEG, 2026-10-21). His classification for the NEXT academic year "
    "(2027-28) is Junior on US forms by his own rule (one year past Sophomore), although 2027-28 is the last year of his "
    "degree: 'Junior' logged from education.class_standing_next is the intended value, never a wrong one, and never ask "
    "for Senior there (Nissan, 2026-10-23). A question about the education he is CURRENTLY "
    "PURSUING takes only his Bachelor's: the high school diploma he already holds stays unticked there, and that is "
    "correct (Cisco, 2026-10-23). His How-did-you-hear answer is an ordered preference (LinkedIn, then Job Board, then Agency / Search Firm, "
    "then Other): the harness picks the first one the form offers, so 'Job Board' (or a job-board or career-site option) "
    "logged from how_did_you_hear / bank.hear-about is his answer on a form that offers no LinkedIn, never a wrong value "
    "(Verizon, 2026-10-20). A profile-use choice that bundles a talent community or recruiting events with being "
    "considered for other roles (Qualcomm's 'ANY ROLES GLOBALLY ... Talent Community and Events') is decided by his rule "
    "never to join a talent community: 'ONLY THE ROLE I AM APPLYING FOR', logged from policy.may_join_talent_community, is "
    "his answer there and never a wrong value, whatever consent.keep_profile_future_roles says (2026-10-21). "
    "A difference only in letter case, spacing or punctuation ('southwest "
    "airlines' for 'Southwest Airlines') is the same value, never a wrong one (Life Fitness, 2026-10-19); 'N/A', 'NA' and "
    "'n/a' are the same answer wherever a box asks for one of them (LLNL, 2026-10-21). "
    "How to read the log: a value of 'checked' or 'pressed' on a "
    "radio, checkbox or toggle means the option named in that label was selected (the label carries the choice, e.g. "
    "'Authorized to work / Yes'); a value equal to a 'Label* Value' read-back is the portal echoing the label in front "
    "of the pick; '(set)' is a password or code that is never shown. When the log holds several decisions for the same "
    "field, the LAST one is the intended value (the earlier ones were corrected). A log marked 'archived' comes from an earlier "
    "attempt of the same draft that the portal saved (the run resumed at its review page): treat it as the current "
    "intent. The log's format is never a defect by itself. 'pass' only when nothing is missing or wrong on the page. "
    "Put the single most important problem in `bucket` and name the fields. Never invent values; judge only what the "
    "log, outline, page text and screenshot show. A value typed inside a box appears in the OUTLINE (value=\"…\"), "
    "not in the page text, so a box is blank only when the outline shows it empty. A control the outline marks disabled "
    "holds the portal's own value (an end date while 'still a student' is ticked) and cannot be changed: never call it "
    "wrong or missing. A form-level banner from an earlier submit click ('Your form needs corrections', 'Missing entry for "
    "required field: X') stays on screen until the next click: when the outline shows X holding its value now, the banner "
    "is stale; judge the field, not the banner (the portal re-checks on the next click). SuccessFactors keeps its "
    "'Please complete your personal information.' message beside a field after the field is set (CenturyLink, L3Harris, "
    "Westinghouse's Salutation showing 'Mr', 2026-10-23): when the field shows a value, that message is stale and never "
    "fails the draft on its own. "
    "A logged value ending in '…[cut for length]' was shortened in the log: a page value that begins with it holds the "
    "intended text, and the outline may show only the first part of a long box as well (Motorola, 2026-10-20). "
    "Consents, acknowledgments and attestations a form requires to apply (privacy notice, terms, data use, 'I have read "
    "and understood', a policy to follow, a willingness to make a disclosure a law or policy requires such as SEC Rule "
    "206(4)-5 political contributions (TIAA, 2026-10-19), an e-signature with his name and date) are delegated: Surya authorized the "
    "agent to accept them on his behalf, so a ticked acknowledgment is the intended value, never a defect (Waymo, "
    "2026-10-19). An SMS / text-message opt-in is ticked (his choice); marketing email, newsletter and job-alert "
    "opt-ins stay unticked. Letting the company keep his profile or data and consider him for other or future openings "
    "(a talent pool, 'further processing for other job opportunities') is a Yes: his own questionnaire answer "
    "(2026-10-18)."
)

FACT_CHECK_INSTRUCTIONS = (
    " FACT CHECK. You also get the APPLICANT PROFILE (the only true facts about the applicant) and a list of UNCONFIRMED "
    "ANSWERS: values the agent wrote or derived itself, each with the evidence it had. Check every one of them against "
    "the profile and its evidence. Put its label in `unsupported` (with a few words on why) when it states something the "
    "profile and evidence do not support or that contradicts them: an employer, title, project, number, metric, skill, "
    "date, award, eligibility or authorization, availability or hours, location, a commitment, a relationship or "
    "affiliation. An answer derived from a stated fact is supported when it follows from it (a U.S. citizen is not on "
    "OPT; 55 hours a week is within a stated 60-hour maximum; No to 'have you worked for X' when the profile never "
    "names X). The profile's silence supports a No about working for or performing services for a named organization "
    "(as an employee, intern, contractor, consultant or contingent worker, or holding its security badge, or having been "
    "retained by the U.S. Government on a matter relating to it: GDIT, 2026-10-24), being related "
    "to someone at it, having a business or personal relationship with its investors, clients, customers, vendors or the "
    "government officials it deals with (Invesco, 2026-10-20), being its own client, customer, account holder or investor "
    "(current or prospective), he or an immediate family member having received its services (tax, audit, advisory, "
    "banking; UL Solutions' PwC personal-tax question, 2026-10-24), being a member of or affiliated with it, having a conflict "
    "of interest or financial interest with "
    "it, being (or having an immediate family member who is) in a position to influence its decisions, hiring or "
    "business dealings (General Motors, 2026-10-20), taking part in a bid, proposal or procurement competition "
    "involving it, having applied to, interviewed with or otherwise taken part in its recruitment or hiring process "
    "before (his applications are in the application ledger; an answer citing no earlier application to the company "
    "is supported, Ecolab 2026-10-22), planning to attend a listed "
    "conference, serving on or planning to join a board of directors or advisory board, or he or a household member "
    "holding a political, government or regulatory office, or his holding or having applied for a securities, "
    "insurance or other regulatory license or registration (his only credential is the AWS Cloud Practitioner "
    "certificate) (his standing default No, American Express and Northrop Grumman "
    "2026-10-19; Micron and American Century 2026-10-21: the profile lists every role he holds and none is a board seat "
    "or office); otherwise the profile's silence supports a No only about working for, or being related to someone at, a named "
    "organization; it never supports an answer about family or relatives abroad, foreign contacts or travel, citizenship, "
    "legal, criminal, financial, medical or security-clearance history, or an amount of experience: those need an explicit "
    "statement in the profile or the evidence, else they are unsupported. "
    "A term or season picked for a date ('Summer 2028' for a May 2028 graduation) is supported when the form's own "
    "definition of that term covers the month (Datadog, 2026-10-20: the form defines Summer as April-July, so May 2028 is "
    "Summer 2028 there); without such a definition, May is Spring. "
    "He is a U.S. citizen with no other citizenship (stored compliance.other_citizenship No) who lives in Sugar Land, Texas, "
    "and holds no visa: a No to being a citizen, national, permanent resident or resident of any other country, including "
    "restricted or sanctioned countries, is supported (Tencent, 2026-10-20). Family or close friends employed by, or "
    "connected to, the company or its affiliates fall under the default No above. "
    "His stored How-did-you-hear answer is LinkedIn (then job boards, then agency): an answer or essay sentence saying he "
    "found or first saw the posting on LinkedIn, or on a job board or career site, is supported (Dimensional, 2026-10-20). "
    "His availability (questionnaire: availability.earliest_start_date, availability.end_date, availability.duration_preference) "
    "is May 18, 2027 to August 14, 2027, about 12 weeks: start and finish dates inside that window, or that window in other "
    "words ('May 2027 through August 2027'), are supported (Inversion Space, 2026-10-20). "
    "His desired pay is $20 an hour, $41,600 a year (stored compensation.desired_annual, his standing answer): a salary or "
    "pay-range option that contains that amount is supported (GDIT, 2026-10-20: '$0 - $50,000'). The figure must match the "
    "period the form states beside it: 20 with 'Hourly' is right, 41600 with 'Hourly' is wrong (HMH, 2026-10-24). "
    "A statement about the company, its products or the role is supported when the JOB POSTING says it (IXL, 2026-10-18: "
    "the role's duties came from the posting). "
    "A statement about what his résumé contains (a link to his GitHub or project samples, a listed project, skill or "
    "role) is supported when the APPLICANT RÉSUMÉ text shows it (Epic Games, 2026-10-19). "
    "Motivation and opinion ('I am interested in X because ...') need no support beyond the facts they cite. "
    "Willingness to take part in the hiring process itself (interviews, assessments, coding tests, a background or drug "
    "screening, references or transcripts on request) follows from applying: a Yes there is supported (Life Fitness, "
    "2026-10-19). "
    "Evidence that quotes the applicant's OWN STORED ANSWER for that question (he confirmed it) supports an answer that "
    "says the same thing in other words (Lyft, 2026-10-19: 'no accommodation needed' against his stored 'No'). "
    "For a single-choice question listed with several picks, only the last pick counts. When no unconfirmed answers "
    "are listed, return an empty list."
)


def validate_verdict(out) -> bool:
    if not isinstance(out, dict):
        return False
    if out.get("verdict") not in VERDICTS or out.get("bucket") not in BUCKETS:
        return False
    if not isinstance(out.get("notes"), str) or not isinstance(out.get("fields"), list):
        return False
    if "unsupported" in out and not (isinstance(out["unsupported"], list) and all(isinstance(f, str) for f in out["unsupported"])):
        return False
    return all(isinstance(f, str) for f in out["fields"])


def should_judge(reason: str) -> bool:
    return str(reason or "").startswith(("shadow_mode", "review_needed"))


def latest(job_id: int) -> dict | None:
    row = db.connect().execute("SELECT verdict, bucket, notes, model, ts FROM judgements WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    return dict(row) if row else None


def archived_decisions(job_id: int, max_chars: int = 12000) -> str:
    """The newest decisions_attemptN.md the worker archived for this job, as text (empty when none)."""
    from . import records
    try:
        files = sorted(records.job_dir(job_id).glob("decisions_attempt*.md"), key=lambda p: int(p.stem.rsplit("attempt", 1)[1]))
    except Exception:
        return ""
    if not files:
        return ""
    return files[-1].read_text(encoding="utf-8")[:max_chars]


class Judge:
    def __init__(self, client=None, model: str | None = None):
        self.client = client
        self.model = model or SETTINGS.models.classifier

    def _client(self):
        if self.client is None:
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI(api_key=SETTINGS.openai_api_key)
        return self.client

    @staticmethod
    def _log_text(decisions: list[dict], job_id: int | None = None) -> str:
        lines = []
        set_refs: dict[str, set] = {}
        for d in decisions:
            if d.get("source") != "cleared" and str(d.get("value") or "").strip():
                set_refs.setdefault(str(d.get("field_label") or ""), set()).add(d.get("ref"))
        for d in decisions[-150:]:
            if d.get("source") == "cleared" and set_refs.get(str(d.get("field_label") or ""), set()) - {d.get("ref")}:
                # Ursa Major (2026-10-22): 'No' was set on the question's own control, then a sibling control with the
                # same label was cleared; read as the last word, the clear made the judge call the page's 'No' wrong
                continue
            v = str(d.get('value') or '')
            v = v if len(v) <= 2000 else v[:2000] + " …[cut for length]"
            label = str(d.get('field_label') or '')
            if v.strip().lower() in ("checked", "pressed") and " / " in label:
                # "Question / No" = checked is the answer No. Mastercard's 101-character label was cut at 80, the "/ No"
                # was lost, and the judge read 'checked' as Yes (2026-10-20).
                question, option = label.rsplit(" / ", 1)
                lines.append(f"- page {d.get('page')}: {question[:200]} = {option.strip()!r} (the option ticked; source {d.get('source')})")
                continue
            lines.append(f"- page {d.get('page')}: {label[:200]} = {v!r} (source {d.get('source')})")
        if lines:
            return "\n".join(lines)
        # A resumed draft (Workday reopens at Review) records nothing new: the previous attempt's archive is the intent.
        archived = archived_decisions(job_id) if job_id is not None else ""
        if archived:
            return "(archived: the portal saved this draft in an earlier attempt; this run resumed it)\n" + archived
        return "(no decisions recorded)"


    @staticmethod
    def _unconfirmed_text(items: list[dict]) -> str:
        lines = []
        for i, it in enumerate(items, 1):
            ev = f" | evidence: {str(it.get('evidence') or '')[:400]}" if it.get("evidence") else ""
            lines.append(f"{i}. {str(it.get('label') or '')[:120]} = {str(it.get('value') or '')[:1500]!r} (source {it.get('source')}{ev})")
        return "\n".join(lines) or "(none)"

    async def judge(self, job_id: int, decisions: list[dict], screenshot_png: bytes | None, page_text: str,
                    gate: dict | None = None, outline: str = "", *, profile: str = "",
                    unconfirmed: list[dict] | None = None, posting: str = "", resume: str = "",
                    correction: str = "") -> dict | None:
        t0 = time.monotonic()
        try:
            model = assert_allowed(self.model)
        except ModelNotAllowed as e:
            db.event(job_id, "judge:error", {"error": str(e)[:300]})
            return None
        content = [
            {"type": "input_text", "text": "DECISION LOG\n" + self._log_text(decisions, job_id)},
            {"type": "input_text", "text": "SURYA'S OWN ANSWERS (to questions this application asked him)\n" + surya_answers(job_id)},
            {"type": "input_text", "text": "FINAL PAGE OUTLINE (every control with the value it holds now; the page text below does not show "
                                           "what is typed inside boxes)\n" + (outline or "(none)")[:16000]},
            {"type": "input_text", "text": "FINAL PAGE TEXT\n" + (page_text or "")[:12000]},
            {"type": "input_text", "text": "CODE GATE RESULT\n" + json.dumps(gate or {})[:1500]},
        ]
        fact_check = unconfirmed is not None
        if fact_check:
            content += [
                {"type": "input_text", "text": "APPLICANT PROFILE\n" + (profile or "(missing)")[:40000]},
                {"type": "input_text", "text": "JOB POSTING (claims about the company or the role may rest on it)\n" + (posting or "(not captured)")[:10000]},
                {"type": "input_text", "text": "APPLICANT RÉSUMÉ (the file attached to this application, as text; claims about what the "
                                               "résumé contains rest on it)\n" + (resume or "(not available)")[:12000]},
                {"type": "input_text", "text": "UNCONFIRMED ANSWERS\n" + self._unconfirmed_text(unconfirmed)},
            ]
        if correction:
            content.append({"type": "input_text", "text": "CORRECTION\n" + correction[:1500]})
        if screenshot_png:
            content.append({"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(screenshot_png).decode(), "detail": "auto"})
        try:
            resp = await self._client().responses.create(
                model=model, instructions=JUDGE_INSTRUCTIONS + (FACT_CHECK_INSTRUCTIONS if fact_check else ""),
                input=[{"role": "user", "content": content}], store=False,
                safety_identifier=SETTINGS.safety_identifier(),
                text={"format": {"type": "json_schema", "name": "judge", "schema": JUDGE_SCHEMA, "strict": True}})
            out = json.loads(resp.output_text)
        except Exception as e:  # noqa: BLE001 - a judge failure never affects the run
            log.warning("judge failed for job %s: %s", job_id, e)
            db.event(job_id, "judge:error", {"error": str(e)[:300]})
            return None
        if not validate_verdict(out):
            db.event(job_id, "judge:error", {"error": "verdict outside the schema", "out": str(out)[:300]})
            return None
        u = getattr(resp, "usage", None)
        db.event(job_id, "judge:verdict", out, latency_ms=int((time.monotonic() - t0) * 1000), model=getattr(resp, "model", model),
                 tokens_in=getattr(u, "input_tokens", 0) or 0, tokens_out=getattr(u, "output_tokens", 0) or 0)
        with db.tx() as con:
            notes = out["notes"] + (("; unsupported: " + "; ".join(out["unsupported"])) if out.get("unsupported") else "")
            con.execute("INSERT INTO judgements(job_id, verdict, bucket, notes, model, ts) VALUES(?,?,?,?,?,?)",
                        (job_id, out["verdict"], out["bucket"], notes[:2000], model, db.now()))
        return out


# "the review page shows “Anywhere in the United States.”": a value the judge says is on the page
_SHOWN = re.compile(r"\b(shows?|showing|displays?|displaying|reads?|holds?|contains?|lists?|says?|has|is set to|still has)\b"
                    r"[^“\"\n]{0,40}[“\"]([^”\"\n]{2,160})[”\"]", re.I)


_QUOTED = re.compile(r"[“\"]([^”\"\n]{2,160})[”\"]")


def case_only_claims(notes: str | None) -> list[tuple[str, str]]:
    """Pairs of quoted values in the judge's notes that differ only in case, spacing or punctuation."""
    from .answers import norm
    vals = [m.group(1).strip().rstrip(".,;:") for m in _QUOTED.finditer(notes or "")]
    out = []
    for i, a in enumerate(vals):
        for b in vals[i + 1:]:
            if a != b and norm(a) and norm(a) == norm(b) and (a, b) not in out:
                out.append((a, b))
    return out


def phantom_claims(notes: str | None, page: str) -> list[str]:
    """Quoted values the judge's notes say the page shows that the page outline and text do not contain."""
    from .answers import norm
    pn = norm(page or "")
    out = []
    for m in _SHOWN.finditer(notes or ""):
        v = m.group(2).strip()
        nv = norm(v)
        if nv and len(nv) >= 3 and nv not in pn and v not in out:
            out.append(v)
    return out


async def removed_decisions(ex, decisions: list[dict], page_no, page_text: str) -> list[dict]:
    """Decisions made on this page for controls that are no longer on it (a work-history row the run removed) and
    whose value the page does not show: they are not part of the draft. CenturyLink (2026-10-21): 'Career Level =
    Professional/Salaried' from a removed SuccessFactors row failed two checks after the row was gone."""
    from .browser import js
    if page_no is None:
        return []
    mine = [d for d in decisions if d.get("page") == page_no and d.get("ref")]
    if not mine:
        return []
    try:
        present = set(await ex.eval(js.JS_REFS_PRESENT, sorted({str(d["ref"]) for d in mine})) or [])
    except Exception:
        return []
    return [d for d in mine if str(d["ref"]) not in present and str(d.get("value") or "") != "(set)"
            and not _on_page(d.get("value"), page_text)]


def _on_page(value, page: str) -> bool:
    from .answers import norm
    v = norm(str(value or ""))[:60]
    return bool(v) and v in norm(page or "")


def resume_text(resume_file: str | None = None) -> str:
    """The text of the résumé attached to the application (the .txt beside the PDF in the résumé folder), raw."""
    from .config import SETTINGS
    folder = SETTINGS.resume_dir
    stem = Path(str(resume_file)).stem if resume_file else ""
    files = ([folder / f"{stem}.txt"] if stem else []) + sorted(folder.glob("*.txt"))
    for f in files:
        try:
            if f.exists():
                return f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
    return ""


async def judge_page(job: dict, ctx, ex, *, shot: str = "shadow", fact_check: bool = False) -> dict | None:
    """Judge the draft on the page the run stopped at: a screenshot (kept with the job), the page outline with
    box values, the page text, the decision log and the gate result. With `fact_check`, the profile and the
    unconfirmed answers go along too. Logs job:judged. Never raises."""
    from . import profile, records
    from .browser import snapshot as snapmod
    job_id = int(job["id"])
    try:
        png = await ex.screenshot_png()
        records.save_screenshot(job_id, png, shot)
        text = await ex.page_text(20000)
        try:
            outline = snapmod.render(await snapmod.take(ex))
        except Exception:
            outline = ""
        all_decisions = records.decisions(job_id)
        gone = await removed_decisions(ex, all_decisions, getattr(ctx, "page_no", None), outline + "\n" + text)
        dropped = {(str(d.get("field_label") or "")[:120], str(d.get("value") or "")[:1500]) for d in gone}
        kwargs = {}
        if fact_check:
            kwargs = {"profile": profile.text(),
                      # an archived decision (an earlier attempt of this draft) counts only while its value is still on
                      # the page: American Express' old 'Expected graduation term = July - September 2028' field was gone
                      # from the form and still failed every check (2026-10-19)
                      "unconfirmed": [u for u in records.unconfirmed_answers(job_id, job.get("company") or "", job.get("ats") or "")
                                      if (not u.get("archived") or _on_page(u.get("value"), outline + "\n" + text))
                                      and (u.get("label"), u.get("value")) not in dropped],
                      "posting": str(getattr(ctx, "posting_text", "") or ""),
                      "resume": resume_text(getattr(ctx, "resume_file", None))}
        judge = Judge(client=getattr(getattr(ctx, "classifier", None), "client", None))
        decisions = [d for d in all_decisions if d not in gone]
        verdict = await judge.judge(job_id, decisions, png, text, getattr(ctx, "gate_result", None), outline=outline, **kwargs)
        # A failing verdict that says the page shows a value the page does not contain is asked once more: Northrop
        # Grumman's Review page showed "Aurora, CO" and the judge failed it for showing "Anywhere in the United
        # States" (2026-10-19)
        failing = verdict.get("notes") if verdict and verdict.get("verdict") != "pass" else ""
        ghost = phantom_claims(failing, outline + "\n" + text)
        same = case_only_claims(failing)
        if same and not ghost:
            db.event(job_id, "judge:case_only", {"pairs": same[:4]})
            again = await judge.judge(job_id, decisions, png, text, getattr(ctx, "gate_result", None), outline=outline,
                                      correction=("Your previous verdict failed values that differ only in letter case, spacing or "
                                                  "punctuation (" + "; ".join(f"{a!r} / {b!r}" for a, b in same[:4]) + "). Those are "
                                                  "the same value: judge the draft again without counting such differences."), **kwargs)
            if again is not None:
                verdict = again
        if ghost:
            db.event(job_id, "judge:phantom", {"claims": ghost[:4]})
            again = await judge.judge(job_id, decisions, png, text, getattr(ctx, "gate_result", None), outline=outline,
                                      correction=("Your previous verdict said the page shows " + "; ".join(repr(g) for g in ghost[:4])
                                                  + ", but neither the FINAL PAGE OUTLINE nor the FINAL PAGE TEXT contains that. Judge "
                                                  "only what the outline, the page text and the screenshot show now; the decision log "
                                                  "says what was intended, the page says what is there."), **kwargs)
            if again is not None:
                verdict = again
    except Exception as e:  # noqa: BLE001 - the judge is evidence, never a crash
        db.event(job_id, "judge:error", {"error": str(e)[:300]})
        return None
    if verdict:
        db.event(job_id, "job:judged", {"verdict": verdict["verdict"], "bucket": verdict["bucket"], "notes": verdict["notes"][:500],
                                        "fields": verdict.get("fields", [])[:12], "unsupported": verdict.get("unsupported", [])[:12]})
    return verdict
