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
    },
    "required": ["verdict", "bucket", "notes", "fields"],
    "additionalProperties": False,
}

JUDGE_INSTRUCTIONS = (
    "You audit a finished draft of an online job application before it is submitted. You get the decision log (every "
    "field the agent set: label, value it read back, the source of that value), the text of the final page and a "
    "screenshot. Decide whether every required field holds the intended value: the value in the log, in the portal's "
    "own wording where the portal rewords options. 'pass' only when nothing is missing or wrong. Put the single most "
    "important problem in `bucket` and name the fields. Never invent values; judge only what the log and page show."
)


def validate_verdict(out) -> bool:
    if not isinstance(out, dict):
        return False
    if out.get("verdict") not in VERDICTS or out.get("bucket") not in BUCKETS:
        return False
    if not isinstance(out.get("notes"), str) or not isinstance(out.get("fields"), list):
        return False
    return all(isinstance(f, str) for f in out["fields"])


def should_judge(reason: str) -> bool:
    return str(reason or "").startswith("shadow_mode")


def latest(job_id: int) -> dict | None:
    row = db.connect().execute("SELECT verdict, bucket, notes, model, ts FROM judgements WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    return dict(row) if row else None


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
    def _log_text(decisions: list[dict]) -> str:
        lines = []
        for d in decisions[-150:]:
            lines.append(f"- page {d.get('page')}: {str(d.get('field_label') or '')[:80]} = {str(d.get('value') or '')[:120]!r} (source {d.get('source')})")
        return "\n".join(lines) or "(no decisions recorded)"

    async def judge(self, job_id: int, decisions: list[dict], screenshot_png: bytes | None, page_text: str,
                    gate: dict | None = None) -> dict | None:
        t0 = time.monotonic()
        try:
            model = assert_allowed(self.model)
        except ModelNotAllowed as e:
            db.event(job_id, "judge:error", {"error": str(e)[:300]})
            return None
        content = [
            {"type": "input_text", "text": "DECISION LOG\n" + self._log_text(decisions)},
            {"type": "input_text", "text": "FINAL PAGE TEXT\n" + (page_text or "")[:12000]},
            {"type": "input_text", "text": "CODE GATE RESULT\n" + json.dumps(gate or {})[:1500]},
        ]
        if screenshot_png:
            content.append({"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(screenshot_png).decode(), "detail": "auto"})
        try:
            resp = await self._client().responses.create(
                model=model, instructions=JUDGE_INSTRUCTIONS, input=[{"role": "user", "content": content}], store=False,
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
            con.execute("INSERT INTO judgements(job_id, verdict, bucket, notes, model, ts) VALUES(?,?,?,?,?,?)",
                        (job_id, out["verdict"], out["bucket"], out["notes"][:2000], model, db.now()))
        return out
