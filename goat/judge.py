"""Judge: one gpt-6-luna verdict per finished shadow draft (roadmap 2026-10-18 item 5).

After the gate passes in shadow mode the run stops before the click. The judge then reads the decision
log and the final screenshot and says whether every required field holds the intended value, with a
bucketed reason when not. The verdict is stored with the job (judgements table), logged as events and
shown by `goat show`, the dashboard and the Telegram finish message. It never raises and is never
called on a live submission."""
from __future__ import annotations

import base64
import json
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

JUDGE_INSTRUCTIONS = (
    "You audit a finished draft of an online job application before it is submitted. You get the decision log (every "
    "field the agent set: label, value it read back, the source of that value), the text of the final page and a "
    "screenshot. Decide whether every required field holds the intended value: the value in the log, in the portal's "
    "own wording where the portal rewords options. How to read the log: a value of 'checked' or 'pressed' on a "
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
    "is stale; judge the field, not the banner (the portal re-checks on the next click)."
)

FACT_CHECK_INSTRUCTIONS = (
    " FACT CHECK. You also get the APPLICANT PROFILE (the only true facts about the applicant) and a list of UNCONFIRMED "
    "ANSWERS: values the agent wrote or derived itself, each with the evidence it had. Check every one of them against "
    "the profile and its evidence. Put its label in `unsupported` (with a few words on why) when it states something the "
    "profile and evidence do not support or that contradicts them: an employer, title, project, number, metric, skill, "
    "date, award, eligibility or authorization, availability or hours, location, a commitment, a relationship or "
    "affiliation. An answer derived from a stated fact is supported when it follows from it (a U.S. citizen is not on "
    "OPT; 55 hours a week is within a stated 60-hour maximum; No to 'have you worked for X' when the profile never "
    "names X). The profile's silence supports a No only about working for, or being related to someone at, a named "
    "organization; it never supports an answer about family or relatives abroad, foreign contacts or travel, citizenship, "
    "legal, criminal, financial, medical or security-clearance history, or an amount of experience: those need an explicit "
    "statement in the profile or the evidence, else they are unsupported. "
    "A statement about the company, its products or the role is supported when the JOB POSTING says it (IXL, 2026-10-18: "
    "the role's duties came from the posting). "
    "Motivation and opinion ('I am interested in X because ...') need no support beyond the facts they cite. "
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
        for d in decisions[-150:]:
            lines.append(f"- page {d.get('page')}: {str(d.get('field_label') or '')[:80]} = {str(d.get('value') or '')[:600]!r} (source {d.get('source')})")
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
                    unconfirmed: list[dict] | None = None, posting: str = "") -> dict | None:
        t0 = time.monotonic()
        try:
            model = assert_allowed(self.model)
        except ModelNotAllowed as e:
            db.event(job_id, "judge:error", {"error": str(e)[:300]})
            return None
        content = [
            {"type": "input_text", "text": "DECISION LOG\n" + self._log_text(decisions, job_id)},
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
                {"type": "input_text", "text": "UNCONFIRMED ANSWERS\n" + self._unconfirmed_text(unconfirmed)},
            ]
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
        kwargs = {}
        if fact_check:
            kwargs = {"profile": profile.text(),
                      "unconfirmed": records.unconfirmed_answers(job_id, job.get("company") or "", job.get("ats") or ""),
                      "posting": str(getattr(ctx, "posting_text", "") or "")}
        verdict = await Judge(client=getattr(getattr(ctx, "classifier", None), "client", None)).judge(
            job_id, records.decisions(job_id), png, text, getattr(ctx, "gate_result", None), outline=outline, **kwargs)
    except Exception as e:  # noqa: BLE001 - the judge is evidence, never a crash
        db.event(job_id, "judge:error", {"error": str(e)[:300]})
        return None
    if verdict:
        db.event(job_id, "job:judged", {"verdict": verdict["verdict"], "bucket": verdict["bucket"], "notes": verdict["notes"][:500],
                                        "fields": verdict.get("fields", [])[:12], "unsupported": verdict.get("unsupported", [])[:12]})
    return verdict
