"""goat.creds.bitwarden against a fake `bw` subprocess layer and a temp database."""
from __future__ import annotations

import base64
import json
import logging
import subprocess

import pytest

from goat import db
from goat.creds import bitwarden as bw

PW = "Fk9#qL2v!Zp8@Wn4^Rt6&Yh3"  # what the fake `bw generate` hands out (24 chars)


class FakeBW:
    """Simulates the handful of bw commands the module uses. Records every argv."""

    def __init__(self, status="unlocked", passwords=None):
        self.status = status
        self.items: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        self.next_id = 100
        self.passwords = passwords or {}
        self.server = None
        self.lock_before_next_get = False   # the session lapses after `bw status` said unlocked

    def __call__(self, argv: list[str], env: dict) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        self.envs.append(dict(env))
        assert argv[0] == bw.SETTINGS.creds.bw_exe
        assert argv[-1] == "--nointeraction"
        args = argv[1:-1]

        def ok(out: str) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(argv, 0, out + "\n", "")

        def fail(code: int = 1) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(argv, code, "", "Not found.")

        if args == ["status"]:
            return ok(json.dumps({"status": self.status, "userEmail": "x@y.z"}))
        if args[:2] == ["config", "server"]:
            self.server = args[2]
            return ok("Saved setting `config`.")
        if args == ["login", "--apikey"]:
            if not (env.get("BW_CLIENTID") and env.get("BW_CLIENTSECRET")):
                return subprocess.CompletedProcess(argv, 1, "", "client_id is required.")
            self.status = "locked"
            return ok("You are logged in!")
        if args[:1] == ["unlock"]:
            self.status = "unlocked"
            return ok("SESSION-FROM-UNLOCK")
        if args == ["generate", "-ulns", "--length", "24"]:
            return ok(PW)
        if args == ["get", "template", "item"]:
            return ok(json.dumps({"organizationId": None, "collectionIds": None, "folderId": None,
                                  "type": 1, "name": "Item name", "notes": "Some notes about this item.",
                                  "favorite": False, "fields": [], "login": None, "secureNote": None,
                                  "card": None, "identity": None, "reprompt": 0}))
        if args[:2] == ["create", "item"]:
            if env.get("BW_SESSION") is None and self.status != "unlocked":
                return fail()
            item = json.loads(base64.b64decode(args[2]))
            item_id = f"item-{self.next_id}"
            self.next_id += 1
            self.items[item_id] = item
            return ok(json.dumps({**item, "id": item_id, "revisionDate": "2026-10-14T00:00:00Z"}))
        if args[:2] == ["list", "items"]:
            term = (args[3] if len(args) > 3 else "").lower()
            out = []
            for iid, it in self.items.items():
                login = it.get("login") or {}
                uris = " ".join((u.get("uri") or "") for u in (login.get("uris") or []))
                if term in f"{it.get('name', '')} {uris}".lower():
                    out.append({**it, "id": iid, "revisionDate": "2026-10-14T00:00:00Z"})
            return ok(json.dumps(out))
        if args[:2] == ["get", "password"]:
            if self.lock_before_next_get:
                self.lock_before_next_get = False
                self.status = "locked"
            if self.status != "unlocked":
                return subprocess.CompletedProcess(argv, 1, "", "Vault is locked.")
            item_id = args[2]
            if item_id in self.items:
                return ok(self.items[item_id]["login"]["password"])
            if item_id in self.passwords:
                return ok(self.passwords[item_id])
            return fail()
        raise AssertionError(f"unexpected bw args: {args}")


@pytest.fixture
def fake(monkeypatch, tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    f = FakeBW()
    monkeypatch.setattr(bw, "_run", f)
    monkeypatch.setattr(bw, "_session", None)
    monkeypatch.setattr(bw.SETTINGS.creds, "bw_session", "ENV-SESSION-123")
    monkeypatch.setattr(bw.SETTINGS.creds, "bw_passwordfile", "")
    monkeypatch.setattr(bw.SETTINGS.creds, "bw_clientid", "")
    monkeypatch.setattr(bw.SETTINGS.creds, "bw_clientsecret", "")
    monkeypatch.setattr(bw.SETTINGS.creds, "bw_server", "")
    # the vault is the credential source only without PASSWORD_BIT (env_login_active)
    monkeypatch.setattr(bw.SETTINGS.creds, "universal_password", "")
    yield f
    db.reset_for_tests(tmp_path / "t2.db")


def test_create_account_credentials_stores_only_item_id(fake, caplog):
    caplog.set_level(logging.DEBUG)
    item_id = bw.create_account_credentials("Acme.wd5.myworkdayjobs.com", "me@example.com")
    assert item_id == "item-100"

    # What went to Bitwarden
    item = fake.items[item_id]
    assert item["name"] == "acme.wd5.myworkdayjobs.com"
    assert item["type"] == 1
    assert item["login"]["username"] == "me@example.com"
    assert item["login"]["password"] == PW
    assert item["login"]["uris"] == [{"uri": "https://acme.wd5.myworkdayjobs.com", "match": 1}]
    assert all(e.get("BW_SESSION") == "ENV-SESSION-123" for e in fake.envs)

    # What went to the DB: host, email, item id -- never the password
    row = bw.lookup("acme.wd5.myworkdayjobs.com")
    assert row == {"host": "acme.wd5.myworkdayjobs.com", "email": "me@example.com",
                   "bw_item_id": "item-100", "created_at": row["created_at"], "last_used_at": None}
    assert isinstance(row["created_at"], int)
    dump = "\n".join(str(tuple(r)) for r in db.connect().execute("SELECT * FROM credentials_index"))
    assert PW not in dump
    assert PW not in repr(row)
    assert PW not in caplog.text and "ENV-SESSION-123" not in caplog.text
    assert "item-100" in caplog.text

    # upsert: same host again replaces the item id rather than failing
    bw.create_account_credentials("acme.wd5.myworkdayjobs.com", "me@example.com")
    assert bw.lookup("acme.wd5.myworkdayjobs.com")["bw_item_id"] == "item-101"
    assert db.connect().execute("SELECT COUNT(*) FROM credentials_index").fetchone()[0] == 1


def test_fill_credentials_existing_calls_callbacks(fake, caplog):
    caplog.set_level(logging.DEBUG)
    bw.create_account_credentials("boards.greenhouse.io", "me@example.com")
    emails, pws = [], []
    status = bw.fill_credentials("boards.greenhouse.io", "me@example.com", emails.append, pws.append, mode="login")
    assert status == "filled"
    assert emails == ["me@example.com"] and pws == [PW]
    assert bw.lookup("boards.greenhouse.io")["last_used_at"] is not None
    assert PW not in caplog.text


def test_fill_credentials_create_mode_creates_then_fills(fake):
    emails, pws = [], []
    assert bw.lookup("jobs.ashbyhq.com") is None
    status = bw.fill_credentials("jobs.ashbyhq.com", "me@example.com", emails.append, pws.append, mode="create")
    assert status == "filled"
    assert emails == ["me@example.com"] and pws == [PW]
    assert bw.lookup("jobs.ashbyhq.com")["bw_item_id"] == "item-100"


def test_workday_signup_reuses_the_universal_item(fake, monkeypatch):
    """One login per ATS family: a new tenant account is created with the SAME password, taken from
    the item named by BW_UNIVERSAL_ITEM, never a freshly generated one."""
    monkeypatch.setattr(bw.SETTINGS.creds, "universal_item", "boeing.wd1.myworkdayjobs.com")
    boeing = bw.create_account_credentials("boeing.wd1.myworkdayjobs.com", "me@example.com")
    other = bw.create_account_credentials("medtronic.wd1.myworkdayjobs.com", "me@example.com")
    assert bw.universal_item("assurant.wd1.myworkdayjobs.com", "me@example.com") == boeing

    emails, pws = [], []
    status = bw.fill_credentials("assurant.wd1.myworkdayjobs.com", "me@example.com", emails.append, pws.append, mode="create")
    assert status == "filled_universal"
    assert fake.calls[-1][1:4] == ["get", "password", boeing]          # Boeing's password, not a new one
    assert emails == ["me@example.com"] and pws == [PW]
    assert bw.lookup("assurant.wd1.myworkdayjobs.com")["bw_item_id"] == boeing

    # login_universal on a tenant with no row of its own uses the same item.
    status = bw.fill_credentials("ge.wd5.myworkdayjobs.com", "me@example.com", emails.append, pws.append, mode="login_universal")
    assert status == "filled_universal"
    assert fake.calls[-1][1:4] == ["get", "password", boeing]
    # a different username in the vault is never borrowed
    assert bw.universal_item("acme.wd1.myworkdayjobs.com", "someone.else@example.com") is None
    assert other != boeing


def test_fill_credentials_login_fallback_and_unknown(fake):
    bw.create_account_credentials("acme.wd5.myworkdayjobs.com", "me@example.com")
    bw.create_account_credentials("zeta.wd1.myworkdayjobs.com", "me@example.com")
    emails, pws = [], []
    status = bw.fill_credentials("other.wd1.myworkdayjobs.com", "me@example.com", emails.append, pws.append, mode="login")
    assert status == "filled_fallback"
    assert emails == ["me@example.com"] and pws == [PW]
    # the FIRST (oldest) row for that registrable domain was used
    assert fake.calls[-1][1:4] == ["get", "password", "item-100"]
    # no row created for the new host; nothing in DB for it
    assert bw.lookup("other.wd1.myworkdayjobs.com") is None

    assert bw.fill_credentials("careers.unknown.com", "me@example.com", emails.append, pws.append, mode="login") == "exists_unknown"
    assert len(pws) == 1  # nothing typed for the unknown host

    with pytest.raises(ValueError):
        bw.fill_credentials("x.com", "me@example.com", emails.append, pws.append, mode="nope")  # type: ignore[arg-type]


def test_fill_confirm_password(fake):
    bw.create_account_credentials("boards.greenhouse.io", "me@example.com")
    pws = []
    assert bw.fill_confirm_password("boards.greenhouse.io", pws.append) == "filled"
    assert pws == [PW]
    assert bw.fill_confirm_password("nobody.example.org", pws.append) == "exists_unknown"
    assert len(pws) == 1


def test_locked_vault_raises_without_passwordfile(fake, monkeypatch):
    fake.status = "locked"
    with pytest.raises(bw.CredentialsUnavailable) as ei:
        bw.ensure_unlocked()
    assert "locked" in str(ei.value)
    fake.status = "unauthenticated"
    with pytest.raises(bw.CredentialsUnavailable):
        bw.create_account_credentials("a.com", "me@example.com")


def test_locked_vault_unlocks_with_passwordfile_and_hides_session(fake, monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    pf = tmp_path / "pw.txt"
    pf.write_text("master\n")
    monkeypatch.setattr(bw.SETTINGS.creds, "bw_passwordfile", str(pf))
    monkeypatch.setattr(bw.SETTINGS.creds, "bw_session", "")
    fake.status = "locked"
    bw.ensure_unlocked()
    assert ["unlock", "--passwordfile", str(pf), "--raw"] == fake.calls[1][1:-1]
    # later calls carry the unlocked session in the environment, and it never hits the log
    bw.create_account_credentials("a.com", "me@example.com")
    assert fake.envs[-1]["BW_SESSION"] == "SESSION-FROM-UNLOCK"
    assert "SESSION-FROM-UNLOCK" not in caplog.text
    assert "SESSION-FROM-UNLOCK" not in repr(bw.lookup("a.com"))


def test_bw_get_failure_hides_output(fake):
    with db.tx() as con:
        con.execute("INSERT INTO credentials_index(host,email,bw_item_id,created_at) VALUES(?,?,?,?)",
                    ("gone.example.com", "me@example.com", "item-missing", 1))
    with pytest.raises(bw.CredentialsUnavailable) as ei:
        bw.fill_credentials("gone.example.com", "me@example.com", lambda s: None, lambda s: None, mode="login")
    assert "Not found" not in str(ei.value)
    assert str(ei.value).startswith("bw get failed")


def test_missing_bw_executable(monkeypatch, tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    monkeypatch.setattr(bw.SETTINGS.creds, "bw_exe", str(tmp_path / "definitely-not-bw.exe"))
    monkeypatch.setattr(bw, "_session", None)
    with pytest.raises(bw.CredentialsUnavailable) as ei:
        bw.status()
    assert "not found" in str(ei.value)


def test_universal_login_never_borrows_another_tenants_password(fake, monkeypatch):
    """The Parsons/GE Vernova bug: a universal login wrote the index, the next call excluded the pinned
    item as this host's 'own', and fell back to the newest item: GE Vernova's generated password."""
    monkeypatch.setattr(bw.SETTINGS.creds, "universal_item", "boeing.wd1.myworkdayjobs.com")
    boeing = bw.create_account_credentials("boeing.wd1.myworkdayjobs.com", "me@example.com")
    gev = bw.create_account_credentials("gevernova.wd5.myworkdayjobs.com", "me@example.com")   # goat-generated, newer
    emails, pws = [], []
    for _ in range(2):
        st = bw.fill_credentials("parsons.wd5.myworkdayjobs.com", "me@example.com", emails.append, pws.append, mode="login_universal")
        assert st == "filled_universal"
        assert fake.calls[-1][1:4] == ["get", "password", boeing]
    assert bw.lookup("parsons.wd5.myworkdayjobs.com") is None              # a login guess is never indexed
    # GE Vernova keeps its own item for exact logins
    st = bw.fill_credentials("gevernova.wd5.myworkdayjobs.com", "me@example.com", emails.append, pws.append, mode="login")
    assert st == "filled" and fake.calls[-1][1:4] == ["get", "password", gev]
    # with the pinned item missing, nothing else is treated as universal
    monkeypatch.setattr(bw.SETTINGS.creds, "universal_item", "missing.wd1.myworkdayjobs.com")
    assert bw.universal_item("parsons.wd5.myworkdayjobs.com", "me@example.com") is None


def test_login_universal_prefers_the_account_created_on_this_tenant(fake, monkeypatch):
    monkeypatch.setattr(bw.SETTINGS.creds, "universal_item", "boeing.wd1.myworkdayjobs.com")
    bw.create_account_credentials("boeing.wd1.myworkdayjobs.com", "me@example.com")
    gev = bw.create_account_credentials("gevernova.wd5.myworkdayjobs.com", "me@example.com")
    emails, pws = [], []
    st = bw.fill_credentials("gevernova.wd5.myworkdayjobs.com", "me@example.com", emails.append, pws.append, mode="login_universal")
    assert st == "filled" and fake.calls[-1][1:4] == ["get", "password", gev]


def _pwfile(tmp_path, monkeypatch):
    pf = tmp_path / "pw.txt"
    pf.write_text("master\n")
    monkeypatch.setattr(bw.SETTINGS.creds, "bw_passwordfile", str(pf))
    return pf


def test_lapsed_session_mid_run_unlocks_again_and_retries(fake, monkeypatch, tmp_path, caplog):
    """A long unattended run: `bw status` said unlocked, then the get found the vault locked. The call
    unlocks with the password file and is retried once; the new session never reaches the log."""
    caplog.set_level(logging.DEBUG)
    pf = _pwfile(tmp_path, monkeypatch)
    bw.create_account_credentials("boards.greenhouse.io", "me@example.com")
    fake.lock_before_next_get = True
    emails, pws = [], []
    st = bw.fill_credentials("boards.greenhouse.io", "me@example.com", emails.append, pws.append, mode="login")
    assert st == "filled" and pws == [PW]
    args = [c[1:-1] for c in fake.calls]
    gets = [i for i, a in enumerate(args) if a[:2] == ["get", "password"]]
    assert len(gets) == 2
    assert args[gets[0] + 1:gets[1]] == [["status"], ["unlock", "--passwordfile", str(pf), "--raw"], ["status"]]
    assert fake.envs[gets[1]]["BW_SESSION"] == "SESSION-FROM-UNLOCK"
    assert "SESSION-FROM-UNLOCK" not in caplog.text and "Vault is locked." not in caplog.text


def test_locked_report_without_passwordfile_fails_once_without_output(fake):
    bw.create_account_credentials("boards.greenhouse.io", "me@example.com")
    fake.lock_before_next_get = True
    with pytest.raises(bw.CredentialsUnavailable) as ei:
        bw.fill_credentials("boards.greenhouse.io", "me@example.com", lambda s: None, lambda s: None, mode="login")
    assert "locked" in str(ei.value) and "Vault is locked." not in str(ei.value)
    assert sum(1 for c in fake.calls if c[1:3] == ["get", "password"]) == 1


def test_unattended_api_key_login_then_unlock(fake, monkeypatch, tmp_path, caplog):
    """The cloud VM: the CLI starts logged out; BW_CLIENTID/BW_CLIENTSECRET log in (through the subprocess
    environment only, never argv or the log), then BW_PASSWORDFILE unlocks."""
    caplog.set_level(logging.DEBUG)
    pf = _pwfile(tmp_path, monkeypatch)
    monkeypatch.setattr(bw.SETTINGS.creds, "bw_session", "")
    monkeypatch.setattr(bw.SETTINGS.creds, "bw_clientid", "user.0f6e-client")
    monkeypatch.setattr(bw.SETTINGS.creds, "bw_clientsecret", "S3cret-API-key-value")
    monkeypatch.setattr(bw.SETTINGS.creds, "bw_server", "https://vault.bitwarden.eu")
    fake.status = "unauthenticated"
    bw.ensure_unlocked()
    assert [c[1:-1] for c in fake.calls] == [
        ["status"], ["config", "server", "https://vault.bitwarden.eu"], ["login", "--apikey"], ["status"],
        ["unlock", "--passwordfile", str(pf), "--raw"], ["status"]]
    assert fake.envs[2]["BW_CLIENTID"] == "user.0f6e-client" and fake.envs[2]["BW_CLIENTSECRET"] == "S3cret-API-key-value"
    assert fake.envs[4].get("BW_CLIENTSECRET") != "S3cret-API-key-value"
    assert not any("S3cret-API-key-value" in a for c in fake.calls for a in c)
    assert "S3cret-API-key-value" not in caplog.text
    # later calls carry the unlocked session
    bw.create_account_credentials("a.com", "me@example.com")
    assert fake.envs[-1]["BW_SESSION"] == "SESSION-FROM-UNLOCK"


def test_logged_out_without_api_key_says_what_to_set(fake):
    fake.status = "unauthenticated"
    with pytest.raises(bw.CredentialsUnavailable) as ei:
        bw.ensure_unlocked()
    assert "BW_CLIENTID" in str(ei.value)
    assert [c[1:-1] for c in fake.calls] == [["status"]]
