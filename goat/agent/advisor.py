"""gpt-6-astra at high effort, called by the harness with a compact failure brief when the gate blocks,
two actions fail in a row, or the page type is unknown. At most ADVISOR_MAX_CALLS per application."""
from __future__ import annotations

import logging
import time

from openai import AsyncOpenAI

from .. import db
from ..config import SETTINGS, assert_allowed
from .prompts import ADVISOR_INSTRUCTIONS

log = logging.getLogger(__name__)


class Advisor:
    def __init__(self, client: AsyncOpenAI | None = None):
        self.client = client or AsyncOpenAI(api_key=SETTINGS.openai_api_key)
        self.calls = 0

    async def advise(self, job_id: int, *, outline: str, last_actions: list[dict], errors: str, gate: str,
                     screenshot_data_url: str | None, answers_text: str = "") -> str | None:
        if self.calls >= SETTINGS.models.advisor_max_calls:
            return None
        self.calls += 1
        content: list[dict] = [{"type": "input_text", "text": (
            f"PAGE OUTLINE:\n{outline[:9000]}\n\nERRORS: {errors[:1500]}\n\nGATE: {gate[:800]}\n\n"
            f"LAST ACTIONS: {last_actions[-8:]}\n\n"
            + (f"SURYA'S STORED ANSWERS (answer keys the agent can use):\n{answers_text[:9000]}\n\n" if answers_text else "")
            + "What should the agent do next?")}]
        if screenshot_data_url:
            content.append({"type": "input_image", "image_url": screenshot_data_url, "detail": "auto"})
        t0 = time.monotonic()
        try:
            resp = await self.client.responses.create(
                model=assert_allowed(SETTINGS.models.advisor),
                reasoning={"effort": SETTINGS.models.advisor_effort},
                instructions=ADVISOR_INSTRUCTIONS,
                input=[{"role": "user", "content": content}],
                store=False,
                safety_identifier=SETTINGS.safety_identifier(),
                max_output_tokens=1500,
            )
            text = resp.output_text
            u = getattr(resp, "usage", None)
            db.event(job_id, "advisor:advice", {"text": text[:2000]}, latency_ms=int((time.monotonic() - t0) * 1000),
                     model=getattr(resp, "model", SETTINGS.models.advisor),
                     tokens_in=getattr(u, "input_tokens", 0) or 0, tokens_out=getattr(u, "output_tokens", 0) or 0)
            return text
        except Exception as e:
            log.warning("advisor failed: %s", e)
            db.event(job_id, "advisor:error", {"error": str(e)[:300]})
            return None
