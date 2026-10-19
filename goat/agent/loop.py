"""The direct Responses API loop. One form page per conversation, from a cached prefix.

- gpt-6.1-sol at low effort; one retry at medium through a `configuration_update` item.
- store=False; the conversation lives here; encrypted reasoning items are sent back verbatim.
- computer_call -> safety-check policy -> per-action target checks -> screenshot back.
- function_call -> ToolDispatcher. A plain-text question is answered from policy or routed.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from openai import APIStatusError, AsyncOpenAI, RateLimitError

from .. import db, memory, records, skills
from ..budget import BudgetExceeded, Parked
from ..browser.executor import ClickRefused, NavigationRefused, to_pw_key
from ..config import SETTINGS, assert_allowed
from . import policy
from .. import profile as profile_mod
from .advisor import Advisor
from .prompts import INSTRUCTIONS, answers_block, job_brief
from .tools import FUNCTION_TOOLS, RunContext, ToolDispatcher, _answer_rows_for_prompt

# Form pages one attempt may pass through before it is held as stuck (a five-step Workday form with every allowed trip
# back stays well under it; TIAA finished in 15).
PAGE_CYCLE_LIMIT = 24

log = logging.getLogger(__name__)

# $/M tokens, list prices (input, cached input, output)
PRICES = {
    "gpt-6.1-sol": (2.0, 0.10, 10.0),
    "gpt-6-astra": (10.0, 1.0, 50.0),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-5.6-sol": (4.0, 0.40, 20.0),
}


def is_bare_computer_call(o: Any) -> bool:
    """A computer_call with neither an action nor a non-empty batch: the model only wanted to look. Replaying it, even
    rebuilt as a screenshot action, was refused ("exactly one of `action` or `actions`", Stoke Space 2026-10-19)."""
    d = o.model_dump(exclude_none=True) if hasattr(o, "model_dump") else dict(o)
    if d.get("type") != "computer_call":
        return False
    acts = d.get("actions")
    if isinstance(acts, dict):
        acts = acts.get("actions")
    return not acts and not d.get("action")


def echo_items(output: list) -> list[dict]:
    """The response's output items as input items for the next request. A bare computer_call is left out, with the
    reasoning item right before it (the API refuses a reasoning item without its following item); the caller shows
    the screen as a user message instead."""
    out: list[dict] = []
    for o in output:
        if is_bare_computer_call(o):
            if out and out[-1].get("type") == "reasoning":
                out.pop()
            continue
        out.append(echo_item(o))
    return out


def echo_item(o: Any) -> dict:
    """A response output item as an input item for the next request. A computer_call must carry exactly
    one of `action` / `actions` (the API rejects both or neither, which ended a T-Mobile run): the batch
    wins when present, then the single action; a bare call with neither was the model looking at the
    screen, so it is echoed as a screenshot action."""
    d = o.model_dump(exclude_none=True) if hasattr(o, "model_dump") else dict(o)
    if d.get("type") == "computer_call":
        if d.get("actions"):
            d.pop("action", None)
        elif d.get("action"):
            d.pop("actions", None)
        else:
            d.pop("actions", None)
            d["action"] = {"type": "screenshot"}
    return d


def estimate_cost(model: str, tokens_in: int, cached: int, out: int) -> float:
    base = next((v for k, v in PRICES.items() if model.startswith(k)), (2.0, 0.1, 10.0))
    return ((tokens_in - cached) * base[0] + cached * base[1] + out * base[2]) / 1e6


@dataclass
class LoopResult:
    outcome: str                 # submitted | parked | budget | error | page_done
    reason: str = ""
    state: str = ""
    blocked_on: list[str] = field(default_factory=list)
    steps: int = 0


class AgentLoop:
    def __init__(self, ctx: RunContext, client: AsyncOpenAI | None = None, advisor: Advisor | None = None):
        self.ctx = ctx
        self.client = client or AsyncOpenAI(api_key=SETTINGS.openai_api_key, max_retries=3)
        self.advisor = advisor or Advisor(self.client)
        self.tools = ToolDispatcher(ctx)
        self.model = SETTINGS.models.executor
        self.effort = SETTINGS.models.executor_effort
        self.detail = SETTINGS.browser.screenshot_detail
        self.items: list[dict] = []
        self.steps = 0
        self.retried_step = False
        self.plain_text_questions = 0
        self.idle_steps = 0          # consecutive model steps without a tool call

    # ------------------------------------------------------------------ prefix
    def _prefix(self, attempt_history: list[str]) -> list[dict]:
        rows = _answer_rows_for_prompt(self.ctx.job.get("company", ""), self.ctx.portal)
        # config/profile.md is everything known about Surya for drafting; surya_facts.md is the fallback.
        facts = profile_mod.text(40000)
        if not facts:
            fp = SETTINGS.config_dir / "surya_facts.md"
            facts = fp.read_text(encoding="utf-8")[:16000] if fp.exists() else ""
        blocks = [
            {"role": "developer", "content": [{"type": "input_text", "text": INSTRUCTIONS}]},
            {"role": "developer", "content": [{"type": "input_text", "text": "PROFILE (everything known about Surya; the only source for drafted text):\n" + facts}]},
            {"role": "developer", "content": [{"type": "input_text", "text": answers_block(rows)}]},
        ]
        # Procedures for this portal (skills/*.md) and what recent runs taught (data/memory): both are stable
        # within a job, so they sit in the cached prefix before the job brief.
        sk = skills.for_filler(self.ctx.portal)
        if sk:
            blocks.append({"role": "developer", "content": [{"type": "input_text", "text": sk}]})
        mem = memory.snapshot(2500)
        if mem:
            blocks.append({"role": "developer", "content": [{"type": "input_text", "text": mem}]})
        blocks.append({"role": "developer", "content": [{"type": "input_text", "text": job_brief(self.ctx.job, attempt_history)}]})
        return blocks

    async def _screenshot_output(self, call_id: str, acks: list[dict]) -> dict:
        data_url = await self.ctx.ex.screenshot_data_url()
        out: dict = {"type": "computer_call_output", "call_id": call_id,
                     "output": {"type": "computer_screenshot", "image_url": data_url}}
        if self.detail:
            out["output"]["detail"] = self.detail
        if acks:
            out["acknowledged_safety_checks"] = acks
        return out

    # ------------------------------------------------------------------ request
    async def _create(self, items: list[dict]) -> Any:
        kwargs: dict = dict(
            model=assert_allowed(self.model),
            reasoning={"effort": self.effort},
            tools=[{"type": "computer"}, *FUNCTION_TOOLS],
            input=items,
            store=False,
            parallel_tool_calls=False,
            safety_identifier=SETTINGS.safety_identifier(),
            include=["reasoning.encrypted_content"],
            truncation="disabled",
        )
        if SETTINGS.models.service_tier and SETTINGS.models.service_tier != "default":
            kwargs["service_tier"] = SETTINGS.models.service_tier
        t0 = time.monotonic()
        for attempt in range(4):
            try:
                resp = await self.client.responses.create(**kwargs)
                break
            except RateLimitError as e:
                wait = float(getattr(getattr(e, "response", None), "headers", {}).get("retry-after", 0) or 0) or (5 * (attempt + 1))
                log.warning("429 from OpenAI; sleeping %.0fs", wait)
                await asyncio.sleep(wait)
            except APIStatusError as e:
                msg = str(e)
                if self.detail and "detail" in msg and e.status_code == 400:
                    # Phase-0 dispute: the screenshot `detail` field. Drop it and retry once.
                    log.warning("API rejected screenshot detail=%s; dropping the field", self.detail)
                    self.detail = ""
                    for it in items:
                        if it.get("type") == "computer_call_output":
                            it["output"].pop("detail", None)
                    continue
                if kwargs.get("service_tier") and e.status_code in (429, 503):
                    kwargs.pop("service_tier", None)  # Flex unavailable -> Standard
                    continue
                raise
        else:
            raise RuntimeError("OpenAI request kept failing")
        u = getattr(resp, "usage", None)
        tin = getattr(u, "input_tokens", 0) or 0
        cached = getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0
        tout = getattr(u, "output_tokens", 0) or 0
        served = getattr(resp, "model", self.model)
        db.event(self.ctx.job_id, "model:step", {"effort": self.effort, "n_output": len(resp.output)},
                 latency_ms=int((time.monotonic() - t0) * 1000), model=served, tokens_in=tin, tokens_cached=cached,
                 tokens_out=tout, cost_usd=estimate_cost(served, tin, cached, tout))
        return resp

    # ------------------------------------------------------------------ computer actions
    async def _run_actions(self, call) -> None:
        ex = self.ctx.ex
        actions = []
        if getattr(call, "actions", None):
            actions = list(getattr(call.actions, "actions", None) or call.actions or [])
        elif getattr(call, "action", None) is not None:
            actions = [call.action]
        for a in actions:
            t = getattr(a, "type", None) or (a.get("type") if isinstance(a, dict) else None)
            g = (lambda k, d=None: getattr(a, k, None) if not isinstance(a, dict) else a.get(k, d))
            if t == "click":
                await ex.click_xy(int(g("x")), int(g("y")), button={"left": "left", "right": "right", "wheel": "middle"}.get(g("button") or "left", "left"),
                                  modifiers=list(g("keys") or []))
            elif t == "double_click":
                await ex.click_xy(int(g("x")), int(g("y")), count=2, modifiers=list(g("keys") or []))
            elif t == "move":
                await ex.move(int(g("x")), int(g("y")))
            elif t == "drag":
                path = [(int(p.x if hasattr(p, "x") else p["x"]), int(p.y if hasattr(p, "y") else p["y"])) for p in (g("path") or [])]
                await ex.drag(path)
            elif t == "scroll":
                await ex.scroll(int(g("x")), int(g("y")), int(g("scroll_x") or 0), int(g("scroll_y") or 0))
            elif t == "keypress":
                await ex.keypress([str(k) for k in (g("keys") or [])])
            elif t == "type":
                text = str(g("text") or "")
                if "\n" in text:
                    raise ClickRefused("refused: typed text contained a newline, which can submit a form")
                await ex.type_text(text)
            elif t == "wait":
                await ex.wait(1.0)
            elif t == "screenshot":
                pass
            else:
                raise ClickRefused(f"unsupported computer action {t}")
            self.ctx.budget.step()
            # Re-check for a challenge after every action: a click that spawned one must not be followed.
        await ex.settle(quiet_ms=300, timeout_ms=3000)

    # ------------------------------------------------------------------ main
    async def run_page(self, attempt_history: list[str], opening_note: str = "") -> LoopResult:
        """Drive the model on the current page until it submits, parks, finishes the page, or the budget ends."""
        ctx = self.ctx
        self.items = self._prefix(attempt_history)
        note = opening_note or "Begin: call snapshot, then fill this page."
        self.items.append({"role": "user", "content": [{"type": "input_text", "text": note}]})
        last_failed = False
        while True:
            ctx.budget.check()
            try:
                resp = await self._create(self.items)
            except Exception as e:
                db.event(ctx.job_id, "model:error", {"error": str(e)[:400]})
                records.save_json(ctx.job_id, f"request_failed_{int(time.time())}.json", {"items_tail": self.items[-6:], "error": str(e)})
                return LoopResult("error", f"model request failed: {str(e)[:200]}")
            self.steps += 1
            self.items += echo_items(resp.output)
            produced_tool = False
            step_failed = False
            for item in resp.output:
                typ = getattr(item, "type", None)
                if typ == "computer_call" and is_bare_computer_call(item):
                    # nothing to do but look: the call is not replayed (see echo_items), so the screen goes back as a user message
                    produced_tool = True
                    self.items.append({"role": "user", "content": [
                        {"type": "input_text", "text": "The page as it looks now (computer tool calls must carry an action)."},
                        {"type": "input_image", "image_url": await ctx.ex.screenshot_data_url(), "detail": self.detail or "auto"}]})
                    continue
                if typ == "computer_call":
                    produced_tool = True
                    decision = policy.review_safety_checks(getattr(item, "pending_safety_checks", []) or [], ctx.ex.url, ctx.ex.policy)
                    if decision.park_reason:
                        raise Parked(decision.park_reason, "hold")
                    try:
                        await self._run_actions(item)
                    except (ClickRefused, NavigationRefused) as e:
                        step_failed = True
                        ctx.consecutive_failures += 1
                        self.items.append(await self._screenshot_output(item.call_id, decision.acknowledged))
                        self.items.append({"role": "user", "content": [{"type": "input_text", "text": f"Harness refused that action: {e}"}]})
                        continue
                    self.items.append(await self._screenshot_output(item.call_id, decision.acknowledged))
                    note = await ctx.ex.follow_new_tab()
                    if note:
                        # The screenshot above was the old tab: show the one it is on now.
                        self.items.append({"role": "user", "content": [
                            {"type": "input_text", "text": note},
                            {"type": "input_image", "image_url": await ctx.ex.screenshot_data_url(), "detail": self.detail or "auto"}]})
                elif typ == "function_call":
                    produced_tool = True
                    page_before = ctx.page_no
                    out_text, img = await self.tools.dispatch(item.name, item.arguments)
                    if ctx.page_no != page_before:
                        # A new form page: the worker starts a fresh conversation from the cached prefix.
                        db.event(ctx.job_id, "page:done", {"page": page_before, "next": ctx.page_no})
                        if ctx.page_no > PAGE_CYCLE_LIMIT and not ctx.submitted:
                            # C&S Wholesale (2026-10-19) went round its five Workday steps 25 times (the Review kept
                            # sending it back); with no step cap the wall budget alone would have let it run 90 minutes.
                            raise Parked(f"stuck: {ctx.page_no - 1} form pages went by without reaching a submit; "
                                         "the form keeps sending the run back", "hold")
                        self.items.append({"type": "function_call_output", "call_id": item.call_id, "output": out_text})
                        return LoopResult("page_done", f"page {ctx.page_no}", steps=self.steps)
                    if '"error"' in out_text[:40]:
                        step_failed = True
                    self.items.append({"type": "function_call_output", "call_id": item.call_id, "output": out_text})
                    if img:
                        self.items.append({"role": "user", "content": [
                            {"type": "input_text", "text": f"Image from {item.name}:"},
                            {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(img).decode(), "detail": self.detail or "auto"}]})
                    if item.name == "submit" and ctx.submitted and ctx.submitted.get("status") == "submitted":
                        return LoopResult("submitted", ctx.submitted.get("signal", ""), steps=self.steps)
                elif typ == "message":
                    text = "".join(getattr(c, "text", "") for c in (getattr(item, "content", None) or []) if getattr(c, "type", "") == "output_text")
                    if not text.strip():
                        continue
                    db.event(ctx.job_id, "model:message", {"text": text[:1200]})
                    reply = policy.answer_model_question(text)
                    if reply is None:
                        self.plain_text_questions += 1
                        if self.plain_text_questions > 3:
                            raise Parked("manual_review: the model kept stopping to ask in prose", "hold")
                        reply = ("If this is a question for Surya, call ask_user with it. If the page is finished, call submit. "
                                 "If you cannot proceed, call park with a named reason. Do not answer in prose.")
                    self.items.append({"role": "user", "content": [{"type": "input_text", "text": reply}]})
            if produced_tool:
                self.idle_steps = 0
            else:
                self.idle_steps += 1
                if self.idle_steps >= 6:
                    # American Express (2026-10-19): eight minutes of reasoning-only replies every 3 s, with no step cap
                    raise Parked("stuck: the model returned no tool call six times in a row", "hold")
            if not produced_tool and resp.output and all(getattr(o, "type", "") == "reasoning" for o in resp.output):
                self.items.append({"role": "user", "content": [{"type": "input_text", "text": "Continue with a tool call."}]})

            # Effort escalation: one retry at medium right after a failed step, then back to low.
            if step_failed and not self.retried_step:
                self.retried_step = True
                self.effort = SETTINGS.models.executor_retry_effort
                self.items.append({"type": "configuration_update", "reasoning": {"effort": self.effort}})
            elif not step_failed and self.retried_step and self.effort != SETTINGS.models.executor_effort:
                self.effort = SETTINGS.models.executor_effort
                self.items.append({"type": "configuration_update", "reasoning": {"effort": self.effort}})
                self.retried_step = False

            # Advisor on two consecutive failures.
            if ctx.consecutive_failures >= 2 and self.advisor.calls < SETTINGS.models.advisor_max_calls:
                ctx.consecutive_failures = 0
                outline = ""
                try:
                    from ..browser import snapshot as snapmod
                    snap = await snapmod.take(ctx.ex)
                    outline = snapmod.render(snap, 9000)
                    errors = "; ".join(f"{k}: {v}" for k, v in snap.errors.items() if k != "url" and v)
                except Exception:
                    errors = ""
                shot = None
                try:
                    shot = await ctx.ex.screenshot_data_url()
                except Exception:
                    pass
                try:
                    answers_text = answers_block(_answer_rows_for_prompt(ctx.job.get("company", ""), ctx.portal))
                except Exception:
                    answers_text = ""
                advice = await self.advisor.advise(ctx.job_id, outline=outline, last_actions=ctx.ex.action_log, errors=errors,
                                                   gate=json.dumps(ctx.gate_result or {}), screenshot_data_url=shot,
                                                   answers_text=answers_text)
                if advice:
                    self.items.append({"role": "user", "content": [{"type": "input_text", "text": "ADVISOR (a senior engineer looked at the page):\n" + advice}]})
            last_failed = step_failed
