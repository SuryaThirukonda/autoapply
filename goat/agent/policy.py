"""Safety-check policy and the plain-text question policy for the computer tool.

pending_safety_checks: `malicious_instructions` and `sensitive_domain` always park the job;
`irrelevant_domain` is acknowledged automatically only while the current host is on the job's
allowlist (a GitHub listing that redirects to a Workday tenant is the common false positive)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..budget import Parked
from ..browser.executor import HostPolicy

ASKS_TO_SUBMIT = re.compile(r"(should i (submit|send|click submit)|ready to submit|shall i submit|do you want me to submit|proceed with submission)", re.I)
ASKS_PERMISSION = re.compile(r"\b(may i|should i|do you want me to|would you like me to|shall i|can i proceed|is it ok)\b", re.I)


@dataclass
class SafetyDecision:
    acknowledged: list[dict]
    park_reason: str | None = None


def review_safety_checks(checks: list, current_url: str, hosts: HostPolicy) -> SafetyDecision:
    ack: list[dict] = []
    for c in checks or []:
        code = (getattr(c, "code", None) or (c.get("code") if isinstance(c, dict) else "") or "").lower()
        cid = getattr(c, "id", None) or (c.get("id") if isinstance(c, dict) else "")
        msg = getattr(c, "message", None) or (c.get("message") if isinstance(c, dict) else "") or ""
        if code in ("malicious_instructions", "sensitive_domain"):
            return SafetyDecision(ack, f"safety_check: {code}: {msg[:160]}")
        if code == "irrelevant_domain":
            if hosts.allowed(current_url):
                ack.append({"id": cid, "code": code, "message": msg})
                continue
            return SafetyDecision(ack, f"safety_check: irrelevant_domain off allowlist ({current_url[:120]})")
        # Unknown check codes are not acknowledged blindly.
        return SafetyDecision(ack, f"safety_check: unknown code {code or '?'}: {msg[:160]}")
    return SafetyDecision(ack)


def answer_model_question(text: str) -> str | None:
    """When the model stops to ask in plain text, answer from policy or return None (route to user)."""
    if ASKS_TO_SUBMIT.search(text):
        return ("Do not submit by clicking. When every page is complete, call the `submit` tool; the code gate decides. "
                "If something is missing, fill it or call ask_user.")
    if ASKS_PERMISSION.search(text):
        return ("You already have written pre-approval for filling this application on this ATS host with Surya's stored "
                "answers, uploading his résumé, and using the credentials and email-code helpers. Continue. For anything "
                "outside that list call ask_user, and for a human-verification challenge call park.")
    return None
