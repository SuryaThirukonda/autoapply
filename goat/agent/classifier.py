"""gpt-6-luna with no reasoning: structured outputs for (a) mapping a new question onto an existing
answer key with a confidence, (b) labelling the page type, (c) tie-breaking a confirmation page.
Deterministic checks run first everywhere; the classifier only breaks ties."""
from __future__ import annotations

import json
import logging
import time

from openai import AsyncOpenAI

from .. import db
from ..config import SETTINGS, assert_allowed
from .prompts import CLASSIFIER_INSTRUCTIONS

log = logging.getLogger(__name__)

MAP_SCHEMA = {
    "type": "object",
    "properties": {
        "key": {"type": ["string", "null"], "description": "an existing key from the list, or null"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["key", "confidence", "reason"],
    "additionalProperties": False,
}

YESNO_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "enum": ["Yes", "No"]},
        "clear_yes": {"type": "boolean", "description": "true only when the profile plainly makes the answer Yes"},
        "reason": {"type": "string"},
    },
    "required": ["answer", "clear_yes", "reason"],
    "additionalProperties": False,
}

YESNO_INSTRUCTIONS = (
    "You answer one yes/no screening question for Surya Thirukonda from the facts given, and return only the JSON asked for. "
    "Answer Yes only when the facts plainly make it Yes. When the facts do not cover the question, or it is unclear, the "
    "answer is No: that is his standing instruction for questions about employers, industries, affiliations and relationships."
)

PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "page_type": {"type": "string", "enum": ["login", "account_creation", "form", "review", "confirmation", "error", "job_posting", "challenge", "email_code", "other"]},
        "confidence": {"type": "number"},
    },
    "required": ["page_type", "confidence"],
    "additionalProperties": False,
}


class Classifier:
    def __init__(self, client: AsyncOpenAI | None = None):
        self.client = client or AsyncOpenAI(api_key=SETTINGS.openai_api_key)
        self.model = SETTINGS.models.classifier

    async def _call(self, job_id: int | None, prompt: str, schema: dict, name: str, instructions: str | None = None) -> dict | None:
        t0 = time.monotonic()
        try:
            kwargs = dict(
                model=assert_allowed(self.model),
                instructions=instructions or CLASSIFIER_INSTRUCTIONS,
                input=prompt,
                store=False,
                safety_identifier=SETTINGS.safety_identifier(),
                text={"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}},
            )
            if SETTINGS.models.classifier_effort:
                kwargs["reasoning"] = {"effort": SETTINGS.models.classifier_effort}
            resp = await self.client.responses.create(**kwargs)
            out = json.loads(resp.output_text)
            u = getattr(resp, "usage", None)
            db.event(job_id, f"classifier:{name}", {"out": out}, latency_ms=int((time.monotonic() - t0) * 1000),
                     model=getattr(resp, "model", self.model),
                     tokens_in=getattr(u, "input_tokens", 0) or 0, tokens_out=getattr(u, "output_tokens", 0) or 0)
            return out
        except Exception as e:  # classifier failures are never fatal: the caller falls back to asking
            log.warning("classifier %s failed: %s", name, e)
            db.event(job_id, f"classifier:{name}:error", {"error": str(e)[:300]})
            return None

    async def map_question(self, job_id: int | None, question: str, options: list[str], keys: list[tuple[str, str]]) -> tuple[str | None, float]:
        """keys: (key, short answer preview). Returns (key or None, confidence)."""
        listing = "\n".join(f"- {k}: {a[:60]}" for k, a in keys[:400])
        prompt = (f"Form question: {question!r}\nOptions: {options[:20]}\n\nExisting answer keys (key: stored answer):\n{listing}\n\n"
                  "Which existing key answers this exact question with the same meaning? Return null unless you are sure. "
                  "Do not stretch: 'U.S. citizen' settles work authorization and sponsorship, but 'veteran: No' does not settle a military-spouse question.")
        out = await self._call(job_id, prompt, MAP_SCHEMA, "map_question")
        if not out or not out.get("key"):
            return None, 0.0
        return out["key"], float(out.get("confidence", 0))

    async def yes_no_from_profile(self, job_id: int | None, question: str, options: list[str], facts: str) -> tuple[str, str]:
        """Settle an employer/industry/affiliation yes/no question from the profile. Default No.
        Returns (answer, reason); never raises."""
        prompt = (f"Facts about Surya:\n{facts[:9000]}\n\nForm question: {question!r}\nOptions: {options[:12]}\n\n"
                  "Answer Yes or No for Surya.")
        out = await self._call(job_id, prompt, YESNO_SCHEMA, "yes_no_from_profile", instructions=YESNO_INSTRUCTIONS)
        if not out:
            return "No", "default No (classifier unavailable)"
        if out.get("answer") == "Yes" and out.get("clear_yes"):
            return "Yes", str(out.get("reason", ""))[:200]
        return "No", str(out.get("reason", "default No"))[:200]

    async def page_type(self, job_id: int | None, outline_head: str) -> tuple[str, float]:
        out = await self._call(job_id, f"Page outline:\n{outline_head[:3000]}\n\nLabel the page type.", PAGE_SCHEMA, "page_type")
        if not out:
            return "other", 0.0
        return out["page_type"], float(out.get("confidence", 0))
