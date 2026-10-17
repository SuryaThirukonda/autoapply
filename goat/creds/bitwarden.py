"""Bitwarden CLI (`bw`) helper: generate, store and fill portal passwords; never returns one.

The password is the secret here. Public functions type it through a `type_password` callback
supplied by the browser executor and return only a status word. Inside this module the
password exists only as a local variable in the function that fetched or generated it; the
database holds host, email and the Bitwarden item id, nothing else. `BW_SESSION` goes to the
subprocess environment and is never logged. The subprocess layer is `_run`, which tests
replace with a fake.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
from typing import Callable, Literal

from .. import db
from ..config import SETTINGS
from ..mail.gmail import registrable_domain

log = logging.getLogger("goat.creds")

TIMEOUT_S = 60
Mode = Literal["create", "login", "login_universal"]


class CredentialsUnavailable(RuntimeError):
    """bw is missing, locked, or a command failed. Never carries command output."""


# In-process session obtained from `bw unlock`; overrides SETTINGS.creds.bw_session. Never logged.
_session: str | None = None


# --------------------------------------------------------------------------------------
# Subprocess layer (injectable)
# --------------------------------------------------------------------------------------

def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    """The only place a process is spawned. Tests monkeypatch this symbol."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT_S, env=env)
    except FileNotFoundError:
        raise CredentialsUnavailable(f"bw executable not found ({SETTINGS.creds.bw_exe})") from None
    except subprocess.TimeoutExpired:
        raise CredentialsUnavailable(f"bw timed out after {TIMEOUT_S}s") from None
    except OSError as e:
        raise CredentialsUnavailable(f"bw could not be started ({type(e).__name__})") from None


def _env() -> dict[str, str]:
    env = dict(os.environ)
    session = _session or SETTINGS.creds.bw_session
    if session:
        env["BW_SESSION"] = session
    else:
        env.pop("BW_SESSION", None)
    return env


# What bw prints when the session key no longer opens the vault (it locked, or the CLI was logged out).
LOCKED_MARKERS = ("vault is locked", "not logged in")


def _reports_locked(proc: subprocess.CompletedProcess) -> bool:
    """Inspects the output only to classify the failure; the output itself is never kept or logged."""
    text = f"{proc.stdout or ''}\n{proc.stderr or ''}".lower()
    return any(m in text for m in LOCKED_MARKERS)


def _bw(*args: str, what: str, relock: bool = True, extra_env: dict[str, str] | None = None) -> str:
    """Run `bw <args> --nointeraction` and return trimmed stdout. Failures become
    CredentialsUnavailable carrying only `what` and the exit code (never stdout/stderr, which
    could echo a secret argument). A call that finds the vault locked unlocks it again and is
    retried once, so a long unattended run survives a lapsed session."""
    global _session
    argv = [SETTINGS.creds.bw_exe, *args, "--nointeraction"]
    proc = _run(argv, {**_env(), **(extra_env or {})})
    if proc.returncode != 0 and relock and _reports_locked(proc):
        log.info("creds: bw %s found the vault locked; unlocking again", what)
        _session = None
        ensure_unlocked()
        proc = _run(argv, {**_env(), **(extra_env or {})})
    if proc.returncode != 0:
        raise CredentialsUnavailable(f"bw {what} failed (exit {proc.returncode})")
    return (proc.stdout or "").strip()


# --------------------------------------------------------------------------------------
# Vault state
# --------------------------------------------------------------------------------------

def status() -> str:
    """`bw status` -> "unlocked" | "locked" | "unauthenticated"."""
    out = _bw("status", what="status", relock=False)
    try:
        return str(json.loads(out).get("status") or "unknown")
    except (ValueError, AttributeError):
        raise CredentialsUnavailable("bw status returned no JSON") from None


def _login_apikey() -> None:
    """`bw login --apikey`: the unattended login on the cloud VM. The client id and secret travel in the
    subprocess environment (bw reads BW_CLIENTID / BW_CLIENTSECRET), never in argv or the log."""
    c = SETTINGS.creds
    if c.bw_server:
        _bw("config", "server", c.bw_server, what="config server", relock=False)
    _bw("login", "--apikey", what="login --apikey", relock=False,
        extra_env={"BW_CLIENTID": c.bw_clientid, "BW_CLIENTSECRET": c.bw_clientsecret})
    log.info("creds: bw logged in with the API key")


def ensure_unlocked() -> None:
    """Make sure the vault is unlocked: log in with the API key if the CLI is logged out and one is
    configured, then unlock with the password file if one is configured."""
    global _session
    st = status()
    if st == "unlocked":
        return
    if st == "unauthenticated" and SETTINGS.creds.bw_clientid and SETTINGS.creds.bw_clientsecret:
        _login_apikey()
        st = status()
    if st == "locked" and SETTINGS.creds.bw_passwordfile:
        pf = SETTINGS.creds.bw_passwordfile
        if not os.path.isfile(pf):
            raise CredentialsUnavailable("bw vault is locked and BW_PASSWORDFILE does not exist")
        session = _bw("unlock", "--passwordfile", pf, "--raw", what="unlock", relock=False)
        if not session:
            raise CredentialsUnavailable("bw unlock returned no session")
        _session = session
        log.info("creds: bw vault unlocked for this process")
        if status() != "unlocked":
            _session = None
            raise CredentialsUnavailable("bw vault still locked after unlock")
        return
    raise CredentialsUnavailable(
        f"bw vault is {st}" + ("" if st == "locked" else "; run `bw login` or set BW_CLIENTID/BW_CLIENTSECRET")
        + ("; set BW_SESSION or BW_PASSWORDFILE" if st == "locked" else "")
    )


# --------------------------------------------------------------------------------------
# Secret-carrying internals (each returns the secret to its caller inside this module only)
# --------------------------------------------------------------------------------------

def generate_password() -> str:
    """Internal: `bw generate -ulns --length 24`. Not for callers outside this module."""
    pw = _bw("generate", "-ulns", "--length", "24", what="generate")
    if len(pw) < 24 or any(ch.isspace() for ch in pw):
        raise CredentialsUnavailable("bw generate returned an unusable value")
    return pw


def _get_password(item_id: str) -> str:
    ensure_unlocked()
    pw = _bw("get", "password", item_id, what="get")
    if not pw:
        raise CredentialsUnavailable("bw get failed")
    return pw


# --------------------------------------------------------------------------------------
# Index (goat.db.credentials_index: host, email, bw_item_id, created_at, last_used_at)
# --------------------------------------------------------------------------------------

def lookup(host: str) -> dict | None:
    """The credentials_index row for an exact host, as a dict. Never contains a password."""
    row = db.connect().execute(
        "SELECT host, email, bw_item_id, created_at, last_used_at FROM credentials_index WHERE host=?",
        (host.lower(),),
    ).fetchone()
    return dict(row) if row is not None else None


def _fallback_row(host: str) -> dict | None:
    """Oldest row sharing the host's registrable domain (one universal account per ATS family)."""
    rd = registrable_domain(host)
    if not rd:
        return None
    rows = db.connect().execute(
        "SELECT host, email, bw_item_id, created_at, last_used_at FROM credentials_index"
        " ORDER BY created_at ASC, host ASC"
    ).fetchall()
    for r in rows:
        if r["host"] != host.lower() and registrable_domain(r["host"]) == rd:
            return dict(r)
    return None


def _neg_date(iso: str) -> tuple:
    """Sort key that puts the NEWEST ISO timestamp first in an ascending sort."""
    return tuple(-ord(c) for c in iso)


def universal_item(host: str, email: str) -> str | None:
    """A vault login for the same registrable domain and username that goat did NOT generate for
    this host. Returns the item id only; the password stays inside _fill."""
    ensure_unlocked()
    rd = registrable_domain(host.lower())
    if not rd:
        return None
    pref = (SETTINGS.creds.universal_item or "").strip().lower()
    # bw's search is fuzzy: "myworkdayjobs.com" does not reliably return every tenant item. Search the
    # bare second-level label too, and the preferred item by name, then merge on id.
    terms = [rd, rd.split(".")[0]]
    if pref:
        terms.append(pref)
    items, seen = [], set()
    for term in terms:
        if not term:
            continue
        raw = _bw("list", "items", "--search", term, what="list")
        try:
            batch = json.loads(raw or "[]")
        except json.JSONDecodeError:
            raise CredentialsUnavailable("bw list returned unparseable output")
        for it in batch:
            if it.get("id") not in seen:
                seen.add(it.get("id"))
                items.append(it)
    def _is_pref(it: dict) -> bool:
        name = (it.get("name") or "").lower()
        uris = " ".join((u.get("uri") or "").lower() for u in ((it.get("login") or {}).get("uris") or []))
        return bool(pref) and (pref in name or pref in uris or it.get("id") == pref)

    if pref:
        pinned = [it for it in items if _is_pref(it)
                  and ((it.get("login") or {}).get("username") or "").strip().lower() == email.strip().lower()]
        if pinned:
            pinned.sort(key=lambda it: _neg_date(it.get("revisionDate") or ""))
            log.info("creds: universal item for %s is the pinned %r", rd, pinned[0].get("name"))
            return pinned[0].get("id")
        log.info("creds: pinned universal item %r not found in the vault", pref)
        return None
    own = {r["bw_item_id"] for r in db.connect().execute("SELECT bw_item_id FROM credentials_index WHERE host=?", (host.lower(),))}
    cands = []
    for it in items:
        login = it.get("login") or {}
        if (login.get("username") or "").strip().lower() != email.strip().lower():
            continue
        uris = [u.get("uri") or "" for u in (login.get("uris") or [])]
        if not any(rd in u.lower() for u in uris):
            continue
        if it.get("id") in own:
            continue
        cands.append(it)
    if not cands:
        return None

    def rank(it: dict) -> tuple:
        name = (it.get("name") or "").lower()
        uris = " ".join((u.get("uri") or "").lower() for u in ((it.get("login") or {}).get("uris") or []))
        preferred = bool(pref) and (pref in name or pref in uris or it.get("id") == pref)
        exact_host = host.lower() in name or host.lower() in uris
        # the pinned universal item first, then this tenant's own item, then the most recently
        # revised one (a password Surya just reset is the newest).
        return (0 if preferred else (1 if exact_host else 2), _neg_date(it.get("revisionDate") or ""))

    cands.sort(key=rank)
    chosen = cands[0]
    log.info("creds: universal item for %s is %r", rd, chosen.get("name"))
    return chosen.get("id")


def _upsert_index(host: str, email: str, item_id: str) -> None:
    with db.tx() as con:
        con.execute(
            "INSERT INTO credentials_index(host,email,bw_item_id,created_at,last_used_at)"
            " VALUES(?,?,?,?,NULL)"
            " ON CONFLICT(host) DO UPDATE SET email=excluded.email, bw_item_id=excluded.bw_item_id,"
            " created_at=excluded.created_at",
            (host.lower(), email, item_id, db.now()),
        )


def _touch(host: str) -> None:
    with db.tx() as con:
        con.execute("UPDATE credentials_index SET last_used_at=? WHERE host=?", (db.now(), host.lower()))


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------

def create_account_credentials(host: str, email: str) -> str:
    """Generate a password, store a login item in Bitwarden for `host`, index it, return the
    item id. The password never leaves this function."""
    ensure_unlocked()
    host = host.lower()
    pw = generate_password()
    try:
        template = json.loads(_bw("get", "template", "item", what="get template"))
    except ValueError:
        raise CredentialsUnavailable("bw get template returned no JSON") from None
    template["type"] = 1  # login
    template["name"] = host
    template["notes"] = None
    template["login"] = {
        "username": email,
        "password": pw,
        "totp": None,
        # match 1 = Host: Workday tenants share a base domain, so Domain matching would
        # offer acme's credentials on every other tenant.
        "uris": [{"uri": "https://" + host, "match": 1}],
    }
    encoded = base64.b64encode(json.dumps(template).encode("utf-8")).decode("ascii")
    del pw, template
    out = _bw("create", "item", encoded, what="create item")
    del encoded
    try:
        item_id = str(json.loads(out)["id"])
    except (ValueError, KeyError, TypeError):
        raise CredentialsUnavailable("bw create item returned no item id") from None
    _upsert_index(host, email, item_id)
    log.info("creds: created bw item %s for host %s", item_id, host)
    return item_id


def _fill(host: str, item_id: str, email: str | None,
          type_email: Callable[[str], None] | None, type_password: Callable[[str], None]) -> None:
    pw = _get_password(item_id)
    if type_email is not None and email is not None:
        type_email(email)
    type_password(pw)
    del pw
    _touch(host)


def fill_credentials(
    host: str,
    email: str,
    type_email: Callable[[str], None],
    type_password: Callable[[str], None],
    mode: Mode,
) -> str:
    """Type email + password for `host` through the callbacks.
    Returns "filled" (exact item, or a freshly created one in create mode),
    "filled_fallback" (login mode: an item from the same registrable domain), or
    "exists_unknown" (login mode: no credentials known for this domain)."""
    if mode not in ("create", "login", "login_universal"):
        raise ValueError(f"mode must be 'create', 'login' or 'login_universal', not {mode!r}")
    host = host.lower()
    if mode == "login_universal":
        # An account goat created on this tenant is recorded in the index with the password it was created
        # with: that is the truth for this host. Otherwise Surya's one universal login for the ATS family.
        own_row = lookup(host)
        if own_row is not None:
            _fill(host, own_row["bw_item_id"], email, type_email, type_password)
            log.info("creds: filled %s from its own item %s", host, own_row["bw_item_id"])
            return "filled"
        item_id = universal_item(host, email)
        if item_id is None:
            log.info("creds: no universal item in the vault for %s", registrable_domain(host))
            return "exists_unknown"
        _fill(host, item_id, email, type_email, type_password)
        log.info("creds: filled %s from universal item %s", host, item_id)
        return "filled_universal"
    row = lookup(host)
    if row is not None:
        _fill(host, row["bw_item_id"], email, type_email, type_password)
        log.info("creds: filled %s from item %s", host, row["bw_item_id"])
        return "filled"
    if mode == "create":
        # One login per ATS family: a new account on a tenant of a family Surya already has an account
        # with is created with that SAME password, not a generated one.
        item_id = universal_item(host, email)
        if item_id is not None:
            _fill(host, item_id, email, type_email, type_password)
            _upsert_index(host, email, item_id)
            log.info("creds: filled %s for sign-up from universal item %s", host, item_id)
            return "filled_universal"
        item_id = create_account_credentials(host, email)
        _fill(host, item_id, email, type_email, type_password)
        log.info("creds: filled %s from new item %s", host, item_id)
        return "filled"
    uid = universal_item(host, email)
    if uid is not None:
        _fill(host, uid, email, type_email, type_password)
        log.info("creds: filled %s from the universal item %s (no item for this host)", host, uid)
        return "filled_fallback"
    fb = _fallback_row(host)
    if fb is None:
        log.info("creds: no credentials known for %s (domain %s)", host, registrable_domain(host))
        return "exists_unknown"
    _fill(fb["host"], fb["bw_item_id"], email, type_email, type_password)
    log.info("creds: filled %s from fallback item %s (host %s)", host, fb["bw_item_id"], fb["host"])
    return "filled_fallback"


def fill_confirm_password(host: str, type_password: Callable[[str], None]) -> str:
    """Type the password again into a "confirm password" box. Returns "filled",
    "filled_fallback" or "exists_unknown"."""
    host = host.lower()
    row = lookup(host)
    if row is not None:
        _fill(host, row["bw_item_id"], None, None, type_password)
        return "filled"
    fb = _fallback_row(host)
    if fb is None:
        return "exists_unknown"
    _fill(fb["host"], fb["bw_item_id"], None, None, type_password)
    return "filled_fallback"


__all__ = [
    "CredentialsUnavailable", "status", "ensure_unlocked", "create_account_credentials",
    "fill_credentials", "fill_confirm_password", "lookup",
]
