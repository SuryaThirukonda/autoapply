"""Red Hat (Workday, 2026-10-23): its reset and sign-up pages want 'A minimum of 14 characters' and PASSWORD_BIT is
shorter. The run reset the password twice, tried to create the account twice and held five minutes later as a
portal_error. A form still demanding a longer password now parks at once, naming the length."""
import pytest

from goat import db
from goat.agent.tools import password_min_length
from goat.budget import Parked
from goat.config import SETTINGS
from tests.test_creds_reset import SignInEx, _with_ex
from tests.test_answer_rules import _job, store  # noqa: F401 - fixtures

RED_HAT = ("Reset Password\nPassword Requirements:\nA special character\nAn uppercase character\nA numeric character\n"
           "A minimum of 14 characters\nPassword*\nError: Password must include:\n- A minimum of 14 characters\nVerify New Password*")


def test_the_minimum_length_a_page_states():
    assert password_min_length(RED_HAT) == 14
    assert password_min_length("Your password must be at least 12 characters long.") == 12
    assert password_min_length("Use 8 or more characters with a mix of letters") == 8
    assert password_min_length("Minimum length: 10 characters") == 10
    assert password_min_length("Password*  Verify New Password*") == 0


@pytest.mark.asyncio
async def test_a_reset_form_that_wants_a_longer_password_parks_at_once(store, monkeypatch):
    monkeypatch.setattr(SETTINGS.creds, "universal_password", "Test-pw-9")          # a test value, 9 characters
    ex = SignInEx(RED_HAT)
    d, ctx = _with_ex(_job(), ex)

    async def probe():
        return {"pw": 2, "title": "Reset Password"}
    monkeypatch.setattr(d, "_account_probe", probe)
    assert await d._password_too_short() == 14
    ctx.creds_mode = "set_password"
    with pytest.raises(Parked) as e:
        await d.t_click("e4", "Submit")
    assert str(e.value).startswith("credentials_unknown:") and "at least 14 characters" in str(e.value)
    assert db.connect().execute("SELECT 1 FROM events WHERE kind='creds:password_too_short'").fetchone()


@pytest.mark.asyncio
async def test_a_long_enough_password_is_left_alone(store, monkeypatch):
    monkeypatch.setattr(SETTINGS.creds, "universal_password", "Test-password-16")   # 16 characters
    d, ctx = _with_ex(_job(), SignInEx(RED_HAT))

    async def probe():
        return {"pw": 2}
    monkeypatch.setattr(d, "_account_probe", probe)
    assert await d._password_too_short() == 0
