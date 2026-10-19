"""The harness, not the model, steers a failed sign-in (one universal password) into the password-reset flow,
and a credentials park waits until that flow was tried."""
from types import SimpleNamespace

import pytest

from goat import db
from goat.adapters import for_portal
from goat.budget import Parked
from tests.test_answer_rules import FakeClassifier, StubEx, _dispatcher, _job, store  # noqa: F401 - fixtures


class SignInEx(StubEx):
    url = "https://acme.wd5.myworkdayjobs.com/en-US/careers/login"

    def __init__(self, text):
        self.text, self.clicks = text, []

    async def click_ref(self, ref, allow_submit=False):
        self.clicks.append(ref)
        return {"tag": "button", "label": "Sign In"}

    async def settle(self, quiet_ms=400, timeout_ms=8000):
        pass

    async def follow_new_tab(self):
        return None

    async def page_text(self, n=0):
        return self.text


def _with_ex(job, ex, classifier=None):
    d, ctx = _dispatcher(job, classifier=classifier)
    ctx.ex = ex
    ctx.last_snapshot = SimpleNamespace(find=lambda ref: {"tag": "button", "label": "Sign In"})
    return d, ctx


@pytest.mark.asyncio
async def test_sign_in_click_after_a_failed_password_points_to_the_reset(store):
    job = _job()
    ex = SignInEx("Sign In\nWrong email address or password. Your account might be locked.")
    d, ctx = _with_ex(job, ex)
    ctx.creds_mode = "login_universal"
    out, _ = await d.t_click("e1", "Sign In")
    assert out["sign_in"] == "failed" and "open_password_reset_link" in out["next"]
    assert ctx.reset_needed and ctx.control_failures["creds:login_failed"] == 1
    ev = db.connect().execute("SELECT detail FROM events WHERE kind='creds:sign_in_failed'").fetchone()
    assert ev and "Wrong email address or password" in ev[0]


@pytest.mark.asyncio
async def test_a_clean_sign_in_click_reports_nothing(store):
    job = _job()
    d, ctx = _with_ex(job, SignInEx("Welcome, Surya. My Information"))
    ctx.creds_mode = "login_universal"
    out, _ = await d.t_click("e1", "Sign In")
    assert "sign_in" not in out and not ctx.reset_needed


@pytest.mark.asyncio
async def test_create_account_on_an_existing_account_points_to_sign_in_then_reset(store):
    job = _job()
    d, ctx = _with_ex(job, SignInEx("An account with this email already exists. Sign in instead."))
    ctx.creds_mode = "create"
    out, _ = await d.t_click("e1", "Create Account")
    assert out["account"] == "exists" and "login_universal" in out["next"] and ctx.reset_needed


@pytest.mark.asyncio
async def test_a_second_login_after_a_failure_is_refused_until_the_reset_ran(store):
    job = _job()
    d, ctx = _with_ex(job, SignInEx(""))
    calls = []
    ctx.creds = SimpleNamespace(fill_credentials=lambda *a: calls.append(a) or "filled_universal", fill_confirm_password=lambda *a: "filled_universal")
    ctx.reset_needed = True
    ctx.control_failures["creds:login_failed"] = 1
    out, _ = await d.t_credentials("login_universal", "e1", "e2", None)
    assert "reset" in out["error"] and calls == []
    ctx.reset_link_tried = True                      # the reset flow ran: a sign-in is allowed again
    out, _ = await d.t_credentials("login_universal", "e1", "e2", None)
    assert out["status"] == "filled_universal" and len(calls) == 1 and ctx.creds_mode == "login_universal"


@pytest.mark.asyncio
async def test_credentials_park_bounces_until_the_reset_link_was_tried(store, monkeypatch):
    from goat.creds import bitwarden
    monkeypatch.setattr(bitwarden, "env_login_active", lambda: False)      # the vault's state no longer matters
    job = _job()
    d, ctx = _with_ex(job, SignInEx("Wrong email address or password"))
    ctx.mail = object()
    ctx.verify_link_tried = True                     # the verification-link branch is covered elsewhere
    for _ in range(2):
        out, _ = await d.t_park("credentials_unknown: wrong password on this tenant")
        assert "Forgot your password" in out["error"]
    ctx.reset_link_tried = True
    with pytest.raises(Parked) as e:
        await d.t_park("credentials_unknown: reset mail never came")
    assert e.value.reason.startswith("credentials_unknown")


@pytest.mark.asyncio
async def test_credentials_park_is_allowed_at_once_without_a_mail_helper(store):
    job = _job()
    d, ctx = _with_ex(job, SignInEx("Wrong email address or password"))
    ctx.mail = None
    ctx.park_attempts = 2
    with pytest.raises(Parked):
        await d.t_park("credentials_unknown: no way to reset")


def test_workday_hints_describe_the_reset_flow():
    h = for_portal("workday").hints
    assert "open_password_reset_link" in h and "set_password" in h
    assert "park with 'credentials_unknown'" not in h and "mode 'login' first" not in h


@pytest.mark.asyncio
async def test_a_tool_that_never_returns_is_stopped_and_reported(store):
    """No tool may hang the single worker: past its cap the call is cancelled and the model told to snapshot."""
    import asyncio
    import json
    job = _job()
    d, ctx = _with_ex(job, SignInEx(""))

    async def t_sleepy():
        await asyncio.sleep(5)
        return {"ok": True}, None
    d.t_sleepy = t_sleepy
    d.TOOL_TIMEOUT_S = 0.2
    text, _ = await d.dispatch("sleepy", "{}")
    assert "did not finish within" in json.loads(text)["error"]
    assert db.connect().execute("SELECT COUNT(*) FROM events WHERE kind='tool:sleepy:timeout'").fetchone()[0] == 1
    from goat.config import SETTINGS
    assert d.tool_timeout_s("await_answers") == SETTINGS.budgets.answer_wait_s + 120
    assert d.tool_timeout_s("click") == 0.2


@pytest.mark.asyncio
async def test_an_unreachable_mailbox_never_breaks_a_park(store):
    """Gmail raised an SSLError inside the park's verification-link check (2026-10-18): the park still happens."""
    job = _job()
    d, ctx = _with_ex(job, SignInEx("Please verify your email"))

    class BrokenMail:
        def fetch_verification_link(self, *a, **k):
            raise RuntimeError("Gmail list failed (SSLError)")
    ctx.mail = BrokenMail()
    ctx.reset_link_tried = True
    ctx.park_attempts = 2
    with pytest.raises(Parked) as e:
        await d.t_park("credentials_unknown: verification email never arrived")
    assert e.value.reason.startswith("credentials_unknown")
