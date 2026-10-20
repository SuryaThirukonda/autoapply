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
    ctx.park_attempts = 4
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
    ctx.park_attempts = 4
    with pytest.raises(Parked) as e:
        await d.t_park("credentials_unknown: verification email never arrived")
    assert e.value.reason.startswith("credentials_unknown")


@pytest.mark.asyncio
async def test_no_reset_email_means_create_the_account_before_a_credentials_park(store):
    """Ryan (2026-10-18): sign-in failed, no reset email came (Workday mails one only for an existing account), and the
    run parked credentials_unknown. Now the harness sends it to Create Account first."""
    job = _job()
    d, ctx = _with_ex(job, SignInEx("Wrong email address or password"))
    ctx.mail = SimpleNamespace(fetch_password_reset_link=lambda portal, host, after: SimpleNamespace(status="not_received", url=None))
    ctx.verify_link_tried = True
    out, _ = await d.t_open_password_reset_link()
    assert out["status"] == "not_received" and "Create Account" in out["next"] and ctx.reset_not_received
    assert db.connect().execute("SELECT COUNT(*) FROM events WHERE kind='creds:reset_not_received'").fetchone()[0] == 1
    out, _ = await d.t_park("credentials_unknown: no reset email")
    assert "Create Account" in out["error"] and "credentials mode 'create'" in out["error"]
    # the account turned out to exist: now the park stands
    ctx.creds_mode = "create"
    ctx.ex = SignInEx("An account with this email already exists.")
    out, _ = await d.t_click("e1", "Create Account")
    assert out["account"] == "exists" and "park credentials_unknown" in out["next"] and ctx.account_exists
    with pytest.raises(Parked) as e:
        await d.t_park("credentials_unknown: account exists, password unknown, no reset email")
    assert e.value.reason.startswith("credentials_unknown")


@pytest.mark.asyncio
async def test_a_create_attempt_also_lets_the_credentials_park_stand(store):
    job = _job()
    d, ctx = _with_ex(job, SignInEx("Something else"))
    calls = []
    ctx.creds = SimpleNamespace(fill_credentials=lambda *a: calls.append(a) or "filled_universal", fill_confirm_password=lambda *a: "filled_universal")
    ctx.mail = object()
    ctx.verify_link_tried = True
    ctx.reset_link_tried = ctx.reset_not_received = True
    await d.t_credentials("create", "e1", "e2", "e3")
    assert ctx.create_tried
    with pytest.raises(Parked):
        await d.t_park("credentials_unknown: create did not go through")


def test_workday_hints_send_a_missing_reset_email_to_create_account():
    h = for_portal("workday").hints
    assert "no reset email" in h and "Create Account" in h and "not_received twice" not in h


def _snap_with(pw_boxes: int):
    items = [{"ref": "e9", "type": "email", "tag": "input", "label": "Email Address"}]
    items += [{"ref": f"p{i}", "type": "password", "tag": "input", "label": "Password"} for i in range(pw_boxes)]
    return SimpleNamespace(items=items, find=lambda ref: {"tag": "button", "label": "Sign In"})


@pytest.mark.asyncio
async def test_credentials_mode_must_match_the_form(store):
    """Wawa (2026-10-19): sign-in credentials typed into the Create Account form, then a reset without a sign-in."""
    job = _job()
    d, ctx = _with_ex(job, SignInEx(""))
    calls = []
    ctx.creds = SimpleNamespace(fill_credentials=lambda *a: calls.append(a) or "filled_universal", fill_confirm_password=lambda *a: "filled_universal")
    ctx.last_snapshot = _snap_with(2)
    out, _ = await d.t_credentials("login_universal", "e9", "p0", None)
    assert "Create Account form" in out["error"] and calls == []
    ctx.last_snapshot = _snap_with(1)
    out, _ = await d.t_credentials("create", "e9", "p0", "p1")
    assert "Sign In form" in out["error"] and calls == []
    out, _ = await d.t_credentials("login_universal", "e9", "p0", None)
    assert out["status"] == "filled_universal" and len(calls) == 1


@pytest.mark.asyncio
async def test_forgot_password_waits_for_a_failed_sign_in(store):
    job = _job()
    d, ctx = _with_ex(job, SignInEx(""))
    ctx.last_snapshot = SimpleNamespace(find=lambda ref: {"tag": "a", "label": "Forgot your password?"})
    out, _ = await d.t_click("e14", "Forgot your password?")
    assert "sign in first" in out["error"]
    ctx.reset_needed = True                          # the sign-in failed: now the reset is the right move
    out, _ = await d.t_click("e14", "Forgot your password?")
    assert "error" not in out



class ProbeEx(SignInEx):
    """A page whose account probe (visible password boxes, headings, title) the test sets."""

    def __init__(self, text, probe):
        super().__init__(text)
        import json as _json
        outer = self

        class _Page:
            async def evaluate(self, script, *a):
                return _json.dumps(outer.probe)
        self.probe = probe
        self.page = _Page()


FORM_OPEN = {"pw": 0, "heads": ["Cloud Media Services Intern", "My Information", "Name"], "title": "Create Account"}


@pytest.mark.asyncio
async def test_a_create_account_click_that_opens_the_form_says_signed_in(store):
    """Tencent (2026-10-20): Create Account went through and the form opened; the run parked credentials_unknown."""
    job = _job()
    d, ctx = _with_ex(job, ProbeEx("My Information How Did You Hear About Us?", FORM_OPEN))
    ctx.creds_mode = "create"
    out, _ = await d.t_click("e1", "Create Account")
    assert out.get("signed_in") and "My Information" in out["next"] and ctx.signed_in and ctx.create_clicked
    assert db.connect().execute("SELECT COUNT(*) FROM events WHERE kind='creds:signed_in'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_a_credentials_park_on_a_signed_in_form_is_bounced(store):
    job = _job()
    d, ctx = _with_ex(job, ProbeEx("My Information", FORM_OPEN))
    ctx.mail = object()
    ctx.verify_link_tried = ctx.reset_link_tried = ctx.reset_not_received = ctx.create_tried = True
    out, _ = await d.t_park("credentials_unknown: account could not be created")
    assert "you are signed in" in out["error"] and "My Information" in out["error"]
    out, _ = await d.t_park("credentials_unknown: account could not be created")
    assert "you are signed in" in out["error"]
    with pytest.raises(Parked):      # bounced twice at most: a third park stands
        await d.t_park("credentials_unknown: account could not be created")


@pytest.mark.asyncio
async def test_a_sign_in_page_right_after_a_create_is_signed_into_first(store):
    """7-Eleven, Mastercard (2026-10-19): Create Account went through, the portal showed Sign In and the run parked."""
    job = _job()
    ex = ProbeEx("Sign In Email Address Password", {"pw": 1, "heads": ["Sign In"], "title": "Careers"})
    d, ctx = _with_ex(job, ex)
    ctx.mail = object()
    ctx.verify_link_tried = ctx.reset_link_tried = ctx.reset_not_received = ctx.create_tried = True
    ctx.creds_mode = "create"
    out, _ = await d.t_click("e1", "Create Account")
    assert ctx.create_clicked and not out.get("signed_in") and "login_universal" in out["next"]
    out, _ = await d.t_park("credentials_unknown: account created but cannot sign in")
    assert "login_universal" in out["error"] and "just created" in out["error"]


@pytest.mark.asyncio
async def test_a_credentials_park_on_a_create_account_page_still_stands(store):
    job = _job()
    d, ctx = _with_ex(job, ProbeEx("Create Account", {"pw": 2, "heads": ["Create Account"], "title": "Create Account"}))
    ctx.mail = object()
    ctx.verify_link_tried = ctx.reset_link_tried = ctx.reset_not_received = ctx.create_tried = ctx.account_exists = True
    with pytest.raises(Parked):
        await d.t_park("credentials_unknown: account exists, password unknown, no reset email")



def test_the_create_pages_own_sign_in_link_is_not_an_existing_account():
    """Analog Devices (2026-10-20): 'Already have an account? Sign In' under the form was read as 'account exists'."""
    from goat.agent.tools import ACCOUNT_EXISTS
    assert not ACCOUNT_EXISTS.search("Create Account Email Address Password Verify New Password Create Account Already have an account? Sign In")
    assert not ACCOUNT_EXISTS.search("Already have an account ? Sign In")
    for msg in ["An account with this email already exists.", "This email address is already in use.",
                "The email address you entered is already registered", "You already have an account. Please sign in.",
                "User already exists"]:
        assert ACCOUNT_EXISTS.search(msg), msg


@pytest.mark.asyncio
async def test_a_create_click_that_leaves_the_form_says_it_did_not_go_through(store):
    job = _job()
    ex = ProbeEx("Create Account Already have an account? Sign In", {"pw": 2, "heads": ["Create Account"], "title": "Create Account"})
    d, ctx = _with_ex(job, ex)
    ctx.creds_mode = "create"
    out, _ = await d.t_click("e1", "Create Account")
    assert out.get("account") != "exists" and not ctx.account_exists and not ctx.create_clicked
    assert "still showing" in out["next"]
    assert db.connect().execute("SELECT COUNT(*) FROM events WHERE kind='creds:create_not_through'").fetchone()[0] == 1


CREATE_FORM = """<!doctype html><html><body style="margin:20px">
<h2>Create Account</h2>
<label for="em">Email Address</label><input id="em" type="text"><br><br>
<label for="p1">Password</label><input id="p1" type="password"><br><br>
<label for="p2">Verify New Password</label><input id="p2" type="password"><br><br>
<button type="button" id="go">Create Account</button>
<div id="menu" style="position:absolute;z-index:99;background:#fff;border:1px solid #888">Unlock your account to view autofill suggestions</div>
<script>
  // a password manager's inline menu that opens under a focused password box and covers the box below it
  document.querySelectorAll('input[type=password]').forEach(el => el.addEventListener('focus', () => {
    const r = el.getBoundingClientRect(); const m = document.getElementById('menu');
    m.style.left = r.left + 'px'; m.style.top = (r.bottom + 2) + 'px'; m.style.width = '320px'; m.style.height = '90px';
  }));
</script>
</body></html>"""


@pytest.mark.asyncio
async def test_the_verify_password_box_is_filled_even_under_a_password_managers_menu(store, tmp_path):
    """Analog Devices (2026-10-20): Bitwarden's inline menu covered 'Verify New Password'; the focusing click hit the
    menu and the box stayed empty."""
    from goat.browser import snapshot
    from goat.browser.executor import Executor, HostPolicy
    page = tmp_path / "create.html"
    page.write_text(CREATE_FORM, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        job = _job()
        d, ctx = _dispatcher(job)
        ctx.ex = ex
        snap = await snapshot.take(ex)
        ctx.last_snapshot = snap
        refs = {i["label"]: i["ref"] for i in snap.items if i.get("label")}
        secret = "Test#Pass123"

        def fill_credentials(host, email, type_email, type_pw, mode):
            type_email(email)
            type_pw(secret)
            return "filled_universal"

        def fill_confirm_password(host, type_pw2):
            type_pw2(secret)
            return "filled"
        ctx.creds = SimpleNamespace(fill_credentials=fill_credentials, fill_confirm_password=fill_confirm_password)
        out, _ = await d.t_credentials("create", refs["Email Address"], refs["Password"], refs["Verify New Password"])
        assert out["status"] == "filled_universal"
        lens = await ex.page.evaluate("() => [document.getElementById('p1').value.length, document.getElementById('p2').value.length]")
        assert lens == [len(secret), len(secret)]
    finally:
        await ex.close(close_browser=True)



@pytest.mark.asyncio
async def test_a_verification_link_is_opened_once_and_the_run_signs_in(store):
    """Simon Property Group, Dee Zee (2026-10-20): the park guard opened the activation link, the model opened it again,
    the sign-in page said 'Invalid Token' and the run parked."""
    job = _job()
    ex = ProbeEx("Sign In Invalid Token Email Address Password", {"pw": 1, "heads": ["Sign In"], "title": "Careers"})
    gone = []

    async def goto(url):
        gone.append(url)
    ex.goto = goto
    ex.url = "https://simon.wd1.myworkdayjobs.com/simon/login"
    d, ctx = _with_ex(job, ex)
    link = "https://simon.wd1.myworkdayjobs.com/simon/activate/abc?redirect=x"
    ctx.mail = SimpleNamespace(fetch_verification_link=lambda portal, host, after, timeout_s=None: SimpleNamespace(status="verified_link", url=link))
    out, _ = await d.t_open_verification_link()
    assert out["status"] == "verified" and gone == [link] and "sign in" in out["next"].lower()
    out, _ = await d.t_open_verification_link()
    assert out["status"] == "already_verified" and gone == [link]
    ctx.reset_link_tried = ctx.reset_not_received = ctx.create_tried = True
    out, _ = await d.t_park("credentials_unknown: Invalid Token after verifying")
    assert "login_universal" in out["error"] and "Invalid Token" in out["error"]



@pytest.mark.asyncio
async def test_a_filled_create_form_is_clicked_before_a_credentials_park(store):
    """GE Vernova (2026-10-20): credentials mode 'create' filled the form and the run parked without clicking it."""
    job = _job()
    d, ctx = _with_ex(job, ProbeEx("Create Account", {"pw": 2, "heads": ["Create Account"], "title": "Create Account"}))
    ctx.mail = object()
    ctx.verify_link_tried = ctx.reset_link_tried = ctx.reset_not_received = ctx.create_tried = True
    ctx.creds_mode = "create"
    out, _ = await d.t_park("credentials_unknown")
    assert "click the Create Account button" in out["error"]



@pytest.mark.asyncio
async def test_the_save_button_on_a_reset_page_is_not_held_back(store):
    """Motorola (2026-10-20): after set_password the 'Submit' button (called 'Reset password save' by the model) was refused."""
    job = _job()
    ex = ProbeEx("Reset Password Password Verify New Password Submit", {"pw": 2, "heads": ["Reset Password"], "title": "Reset Password"})
    d, ctx = _with_ex(job, ex)
    ctx.last_snapshot = SimpleNamespace(find=lambda ref: {"tag": "button", "label": "Submit"})
    ctx.reset_link_tried = True
    ctx.creds_mode = "set_password"
    out, _ = await d.t_click("e4", "Reset password save")
    assert "error" not in out and ex.clicks == ["e4"]



@pytest.mark.asyncio
async def test_a_typed_new_password_is_saved_before_a_credentials_park(store):
    """7-Eleven (2026-10-20): set_password filled the reset form and the run parked without clicking its 'Submit'."""
    job = _job()
    d, ctx = _with_ex(job, ProbeEx("Reset Password Submit", {"pw": 2, "heads": ["Reset Password"], "title": "Reset Password"}))
    ctx.mail = object()
    ctx.verify_link_tried = ctx.reset_link_tried = True
    ctx.creds_mode = "set_password"
    out, _ = await d.t_park("credentials_unknown: the save control is labelled Submit")
    assert "not saved" in out["error"] and "Submit" in out["error"]
