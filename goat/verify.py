"""Verified auto-submit (Surya, 2026-10-18): "if an application has all correct information after filling in, then
just submit it, so it doesnt need to be refilled later. only submit if you can ensure all the info is correct,
otherwise send a notification through telegram to allow submitting".

A draft whose code gate passed in shadow mode is submitted at once only when every check below agrees. Anything
short of that holds the draft as `review_needed`, and Telegram asks Surya with a Submit button:

1. the code gate passed (required fields, a source for every value, attestations from standing answers, the
   Workday Review / Greenhouse completeness checks, no page errors, no CAPTCHA): true by the time this runs;
2. the read-back left no page with a field that shows something other than its decision;
3. no question to Surya is still unanswered;
4. the judge (gpt-6-luna) passes the draft against the decision log, page outline, page text and screenshot;
5. the judge's fact check finds every unconfirmed answer (essays the agent wrote, derived answers, defaults, values
   kept as the page showed them) supported by the profile and the evidence the agent had.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import answers, db, records


# Judge findings the agent can fix itself on the page (a blank required field, a wrong pick, a date that did not
# commit, a missing file); a portal error or a missing verdict is not one of them.
FIXABLE_BUCKETS = ("missing_required", "wrong_value", "wrong_option_wording", "date_not_committed", "file_not_attached",
                   "unsourced_value", "consent_or_attestation", "other")


@dataclass
class Verification:
    ok: bool
    why: list[str] = field(default_factory=list)
    verdict: dict | None = None
    unconfirmed: int = 0
    fixes: list[str] = field(default_factory=list)   # what the agent can fix before Surya is asked
    needs_surya: bool = False                         # something only he can settle (an unanswered question)

    def summary(self) -> str:
        return "; ".join(self.why)[:600]

    @property
    def fixable(self) -> bool:
        """One more try by the agent is worth it: every reason is something it can fix on the page."""
        return bool(self.fixes) and not self.needs_surya and len(self.fixes) >= len(self.why)


def open_readback(ctx) -> list[str]:
    """Fields that still differed from their decisions when the run left their page (t_click records them)."""
    out = []
    for page, mism in sorted((getattr(ctx, "readback_open", None) or {}).items()):
        out += [f"page {page}: {m}" for m in (mism or [])]
    return out


_MISMATCH = __import__("re").compile(r"\[(e\d+)\] shows ('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\") but decision was ('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")")


async def live_open_readback(ctx, ex) -> list[str]:
    """open_readback, without the complaints the page no longer supports: a field that now holds its decided value
    was fixed after its page was left (American Express' Oracle pickers held the right values while the stale
    complaints failed every check, 2026-10-19)."""
    opened = getattr(ctx, "readback_open", None) or {}
    if not opened:
        return []
    try:
        import ast
        from . import readback as rbm
        from .browser import snapshot as snapmod
        snap = await snapmod.take(ex)
    except Exception:
        return open_readback(ctx)
    for page in list(opened):
        keep = []
        for m in opened[page] or []:
            mm = _MISMATCH.search(m)
            item = snap.refs.get(mm.group(1)) if mm else None
            if item is not None:
                try:
                    want = ast.literal_eval(mm.group(3))
                except Exception:
                    want = None
                if want is not None and rbm.values_equal(str(want), rbm._page_value(item), rbm._kind_for(item, str(want)),
                                                         item.get("label") or ""):
                    continue
            keep.append(m)
        if keep:
            opened[page] = keep
        else:
            opened.pop(page, None)
    return open_readback(ctx)


def pending_questions(ctx) -> list[str]:
    company = ctx.job.get("company", "")
    return [k for k in ctx.asked_keys if not ((a := answers.get(k, company, ctx.portal)) and a.has_value)]


async def verify(ctx, ex) -> Verification:
    """Run every check; the judge always runs, so a held draft's Telegram message carries its verdict."""
    from .judge import judge_page
    why: list[str] = []
    fixes: list[str] = []
    rb = await live_open_readback(ctx, ex)
    if rb:
        why.append("read-back: " + "; ".join(rb[:3]))
        fixes += [f"{m} (go back to that page with go_to_step, re-set the field from its answer_key, then return and submit)" for m in rb[:4]]
    pending = pending_questions(ctx)
    if pending:
        why.append("unanswered question(s): " + ", ".join(pending[:4]))
    unconfirmed = records.unconfirmed_answers(ctx.job_id, ctx.job.get("company", ""), ctx.portal)
    verdict = await judge_page(ctx.job, ctx, ex, shot="shadow", fact_check=True)
    ctx.verdict, ctx.judged = verdict, True
    if verdict is None:
        why.append("the judge gave no verdict")
    else:
        if verdict["verdict"] != "pass":
            why.append(f"judge: {verdict['verdict']} ({verdict['bucket']}): {verdict['notes'][:200]}")
            if verdict["bucket"] in FIXABLE_BUCKETS:
                named = ", ".join(str(f)[:60] for f in (verdict.get("fields") or [])[:6])
                fixes.append(f"the judge ({verdict['bucket']}): {verdict['notes'][:300]}" + (f" Fields: {named}." if named else "")
                             + " Fix exactly that from the stored answers (answer_keys), then call submit again.")
        if "unsupported" not in verdict:
            why.append("the fact check did not run")
        elif verdict["unsupported"]:
            why.append("not backed by the profile: " + "; ".join(str(u)[:120] for u in verdict["unsupported"][:4]))
            fixes += [f"not backed by the profile: {str(u)[:200]}. Rewrite or re-set that answer so it states only what the "
                      "profile supports (a stored answer_key where one exists), then call submit again." for u in verdict["unsupported"][:4]]
    v = Verification(ok=not why, why=why, verdict=verdict, unconfirmed=len(unconfirmed), fixes=fixes, needs_surya=bool(pending))
    db.event(ctx.job_id, "verify:" + ("pass" if v.ok else "fail"),
             {"why": why, "unconfirmed": len(unconfirmed), "judge": (verdict or {}).get("verdict"),
              "unsupported": (verdict or {}).get("unsupported", [])[:8]})
    return v
