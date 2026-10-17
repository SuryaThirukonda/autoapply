"""Human-verification detection. Nothing here solves, clicks or bypasses a challenge; it only
tells the worker to finish the form and park it one click from done. It also separates an
emailed verification code page (automated by the mail helper) from a CAPTCHA, the mislabel that
parked National Life in the old pipeline."""
from __future__ import annotations

import re

from . import js
from .executor import Executor

CODE_WORDS = re.compile(r"(verification|security|confirmation|one[- ]time)\s+code", re.I)
CAPTCHA_WORDS = re.compile(r"(hcaptcha|recaptcha challenge|turnstile|puzzle|select all (images|squares)|i'?m not a robot)", re.I)


async def detect_challenge(ex: Executor) -> dict:
    d = await ex.eval(js.JS_CHALLENGE)
    if not isinstance(d, dict):
        return {"challenge": False, "kinds": [], "email_code": False, "frames": []}
    return d


def classify_hold_text(text: str) -> str:
    """'email_code' | 'captcha' | 'other' for free-text reasons (digest, old holds)."""
    if CAPTCHA_WORDS.search(text or ""):
        return "captcha"
    if CODE_WORDS.search(text or ""):
        return "email_code"
    return "other"
