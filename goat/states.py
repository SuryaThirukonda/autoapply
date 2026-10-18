"""The ten job states and their allowed transitions. No state means "unclear"."""
from __future__ import annotations

NEW = "new"
QUEUED = "queued"
FILLING = "filling"
AWAITING_ANSWER = "awaiting_answer"
AWAITING_CODE = "awaiting_code"
GATED = "gated"
SUBMITTING = "submitting"
SUBMITTED = "submitted"
CONFIRMED = "confirmed"
READY_FOR_YOU = "ready_for_you"
HOLD = "hold"
# Listing-level terminal outcomes (never become jobs the worker runs)
TRIAGED_OUT = "triaged_out"
DEAD = "dead"

ALL = {NEW, QUEUED, FILLING, AWAITING_ANSWER, AWAITING_CODE, GATED, SUBMITTING, SUBMITTED,
       CONFIRMED, READY_FOR_YOU, HOLD, TRIAGED_OUT, DEAD}

TRANSITIONS: dict[str, set[str]] = {
    NEW: {TRIAGED_OUT, DEAD, QUEUED},
    QUEUED: {FILLING, DEAD, HOLD},
    FILLING: {AWAITING_ANSWER, AWAITING_CODE, GATED, READY_FOR_YOU, HOLD, DEAD},
    AWAITING_ANSWER: {FILLING, HOLD},
    AWAITING_CODE: {FILLING, HOLD},
    GATED: {SUBMITTING, FILLING, HOLD, READY_FOR_YOU},
    SUBMITTING: {SUBMITTED, FILLING, HOLD},
    SUBMITTED: {CONFIRMED, HOLD},
    READY_FOR_YOU: {SUBMITTED, HOLD, FILLING},
    HOLD: {QUEUED, DEAD, READY_FOR_YOU},
    CONFIRMED: set(),
    TRIAGED_OUT: {QUEUED},
    DEAD: set(),
}

# States a worker may claim.
CLAIMABLE = {QUEUED}
# States that mean "a human has to do something" and show in the digest.
NEEDS_HUMAN = {HOLD, READY_FOR_YOU}


class IllegalTransition(RuntimeError):
    pass


def check(frm: str, to: str) -> None:
    if to not in TRANSITIONS.get(frm, set()):
        raise IllegalTransition(f"{frm} -> {to}")


# Named hold reasons. Every hold carries one of these prefixes (free text after a colon).
HOLD_REASONS = (
    "answer_timeout",            # 15 minutes passed with no reply
    "budget_spent",              # steps / wall clock / failed submits
    "no_standing_answer",        # attestation or personal fact with no standing answer
    "automation_prohibited",     # page says AI/automated applications are disqualifying
    "bot_check_question",        # question addressed to a robot/AI
    "verification_email_not_received",
    "code_not_received",
    "credentials_unknown",       # account exists, no stored credential, reset mail out of scope
    "posting_closed",
    "gate_failed",
    "submit_failed",
    "safety_check",              # malicious_instructions / sensitive_domain / off-allowlist
    "host_mismatch",             # verification link pointed somewhere else
    "eligibility",               # graduation window, degree, clearance, radius...
    "browser_unavailable",
    "application_cap",           # the board's own limit (Waymo 5 per 60 days...); requeued when it opens
    "manual_review",
)
