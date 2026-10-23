"""When the Bitwarden vault is not unlocked and PASSWORD_BIT is set, the credentials helper types
APPLICATION_EMAIL + PASSWORD_BIT on every portal (Surya, 2026-10-18). The value only ever reaches the typing
callback."""
from __future__ import annotations

from goat import db
from goat.config import SETTINGS
from goat.creds import bitwarden


def test_env_login_takes_over_when_the_vault_is_unavailable(tmp_path, monkeypatch):
    db.reset_for_tests(tmp_path / "t.db")
    monkeypatch.setattr(SETTINGS.creds, "universal_password", "pw-for-test")
    monkeypatch.setattr(SETTINGS.creds, "application_email", "app@example.com")
    monkeypatch.setattr(bitwarden, "status", lambda: (_ for _ in ()).throw(bitwarden.CredentialsUnavailable("bw vault is unauthenticated")))
    typed = {}
    out = bitwarden.fill_credentials("acme.wd5.myworkdayjobs.com", "", typed.__setitem__.__get__("email") if False else (lambda v: typed.__setitem__("email", v)),
                                     lambda v: typed.__setitem__("pw", v), "login_universal")
    assert out == "filled_universal"
    assert typed == {"email": "app@example.com", "pw": "pw-for-test"}
    again = {}
    assert bitwarden.fill_confirm_password("acme.wd5.myworkdayjobs.com", lambda v: again.__setitem__("pw", v)) == "filled_universal"
    assert again == {"pw": "pw-for-test"}


def test_env_login_stays_off_without_the_variable(monkeypatch):
    monkeypatch.setattr(SETTINGS.creds, "universal_password", "")
    assert bitwarden.env_login_active() is False


def test_password_bit_wins_over_an_unlocked_vault(tmp_path, monkeypatch):
    """Westinghouse (2026-10-23): with the vault unlocked, sign-in typed the vault item's other password and failed, while
    the reset page said PASSWORD_BIT already was the account's password. PASSWORD_BIT is his one sign-in password."""
    db.reset_for_tests(tmp_path / "t.db")
    monkeypatch.setattr(SETTINGS.creds, "universal_password", "pw-for-test")
    monkeypatch.setattr(SETTINGS.creds, "application_email", "app@example.com")
    monkeypatch.setattr(bitwarden, "status", lambda: "unlocked")
    monkeypatch.setattr(bitwarden, "lookup", lambda host: {"bw_item_id": "vault-item"})
    monkeypatch.setattr(bitwarden, "_get_password", lambda item_id: "the-vault-password")
    assert bitwarden.env_login_active() is True
    typed = {}
    out = bitwarden.fill_credentials("career4.successfactors.com", "", lambda v: typed.__setitem__("email", v),
                                     lambda v: typed.__setitem__("pw", v), "login_universal")
    assert out == "filled_universal" and typed == {"email": "app@example.com", "pw": "pw-for-test"}


def test_set_password_types_the_env_password_only(monkeypatch):
    monkeypatch.setattr(SETTINGS.creds, "universal_password", "pw-for-test")
    typed = {}
    out = bitwarden.fill_credentials("acme.wd5.myworkdayjobs.com", "", lambda v: typed.__setitem__("email", v),
                                     lambda v: typed.__setitem__("pw", v), "set_password")
    assert out == "filled_universal" and typed == {"pw": "pw-for-test"}
    monkeypatch.setattr(SETTINGS.creds, "universal_password", "")
    assert bitwarden.fill_credentials("acme.wd5.myworkdayjobs.com", "", lambda v: None, lambda v: None, "set_password") == "exists_unknown"


def test_sign_in_failed_regex_covers_portal_wordings():
    from goat.agent.tools import ACCOUNT_EXISTS, SIGN_IN_FAILED
    for s in ("Wrong email address or password", "Your account might be locked", "The email address or password you entered is incorrect",
              "Invalid credentials", "Password does not match", "We could not sign you in"):
        assert SIGN_IN_FAILED.search(s), s
    for s in ("Password must be at least 8 characters", "Sign In", "Forgot your password?", "Create Account", "Verify New Password"):
        assert not SIGN_IN_FAILED.search(s), s
    assert ACCOUNT_EXISTS.search("An account with this email already exists") and not ACCOUNT_EXISTS.search("Create your account")
