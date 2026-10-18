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


def test_env_login_defers_to_an_unlocked_vault(monkeypatch):
    monkeypatch.setattr(SETTINGS.creds, "universal_password", "pw-for-test")
    monkeypatch.setattr(bitwarden, "status", lambda: "unlocked")
    assert bitwarden.env_login_active() is False
