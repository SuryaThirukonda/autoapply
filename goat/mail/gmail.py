"""Gmail API read-only helper: emailed verification codes and verification links, portal-scoped.

The code is the secret here. It is never returned to a caller, logged, or placed in an
exception: `fetch_code` hands back a `CodeHandle` whose only way out is `enter(type_into)`,
which types the code through the browser executor's callback and returns the word "entered".
Everything that reads mail goes through a `MailClient` so tests run with a fake.
"""
from __future__ import annotations

import os
import base64
import html as _html
import logging
import re
import threading
import time
from dataclasses import dataclass
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from ..config import SETTINGS

log = logging.getLogger("goat.mail")
# Every Gmail API call holds this: the client and its connection are shared by threads (see GmailClient._execute).
_GMAIL_LOCK = threading.RLock()
_RATE_LIMITED = re.compile(r"rate ?limit|quota|too many requests", re.I)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class MailUnavailable(RuntimeError):
    """Gmail cannot be reached or is not set up. Never carries message content."""


# --------------------------------------------------------------------------------------
# Portal registry
# --------------------------------------------------------------------------------------

SIX_DIGITS = r"\b\d{6}\b"
EIGHT_ALNUM = r"\b[A-Za-z0-9]{8}\b"

# link_host_rule: "same_host" = link host must be the portal host or a subdomain of it (shared
# multi-tenant ATS domains, where a sibling tenant is a different company); "registrable" =
# any host within the portal host's registrable domain (a company's own domain).
PORTALS: dict[str, dict[str, Any]] = {
    "greenhouse": dict(
        senders=["greenhouse-mail.io", "greenhouse.io"],
        subject_hints=["Security code", "verification code"],
        code_regex=EIGHT_ALNUM,
        link_host_rule="same_host",
    ),
    "workday": dict(
        senders=["myworkday.com", "myworkdayjobs.com", "workday.com"],
        subject_hints=["Verify", "verification", "Confirm"],
        code_regex=SIX_DIGITS,
        link_host_rule="same_host",
    ),
    "oracle": dict(
        # 'noreply_fusion_essf@' is Oracle Fusion's mailer on the employer's own domain: Berkshire Hathaway Energy's code
        # came from noreply_fusion_essf@oci.brkenergy.com and was never found (2026-10-22). An entry ending in '@' is a
        # sender name on any domain.
        senders=["oraclecloud.com", "oracle.com", "noreply_fusion_essf@"],
        # onsemi's tenant titles its one-time code email "onsemi Confirm Your Identity".
        subject_hints=["code", "Verify", "verification", "Confirm", "Identity", "passcode", "one-time"],
        code_regex=SIX_DIGITS,
        link_host_rule="same_host",
    ),
    "icims": dict(
        senders=["icims.com"],
        subject_hints=["code", "Verify", "verification"],
        code_regex=SIX_DIGITS,
        link_host_rule="same_host",
    ),
    "ashby": dict(
        senders=["ashbyhq.com"],
        subject_hints=["code", "Verify", "verification", "Confirm"],
        code_regex=SIX_DIGITS,
        link_host_rule="same_host",
    ),
}

# Multi-tenant ATS base domains: a sibling subdomain is another company's tenant.
SHARED_ATS_DOMAINS: frozenset[str] = frozenset(
    {s for p in PORTALS.values() for s in p["senders"] if "@" not in s}
    | {"lever.co", "smartrecruiters.com", "jobvite.com", "taleo.net", "successfactors.com",
       "ultipro.com", "workable.com", "bamboohr.com", "breezy.hr", "applytojob.com",
       "rippling.com", "paylocity.com", "adp.com", "dayforcehcm.com", "phenom.com",
       "eightfold.ai", "avature.net", "teamtailor.com", "recruitee.com", "pinpointhq.com"}
)

# Two-level public suffixes where the registrable domain is three labels long.
TWO_LEVEL_SUFFIXES: frozenset[str] = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "co.in", "co.jp", "co.nz", "co.za", "co.kr",
    "com.au", "net.au", "org.au", "com.br", "com.mx", "com.sg", "com.hk", "com.tw", "com.cn",
    "com.ar", "com.tr", "co.il", "co.id", "com.my", "com.ph",
})

FORBIDDEN_RE = re.compile(
    r"(?i)(password reset|reset your password|account recovery|recover your account|bank|chase|"
    r"wells fargo|capital one|paypal|venmo|zelle|payment|invoice|two-factor|2fa|sign-in attempt|"
    r"new login)"
)

# Eight-letter English words that could pass the Greenhouse 8-alphanumeric pattern.
COMMON_WORDS: frozenset[str] = frozenset({
    "security", "password", "personal", "download", "continue", "complete", "internet",
    "question", "response", "received", "position", "thursday", "saturday", "november",
    "december", "february", "possible", "together", "business", "services", "software",
    "engineer", "yourself", "verified", "verifies", "expiring", "requests", "required",
    "entering", "accounts", "anywhere", "whenever", "applying", "attempts", "activate",
    "activity", "messages", "contacts", "username", "identity", "customer", "settings",
    "practice", "standard", "original", "specific", "whatever", "problems", "included",
    "reserved", "thankyou", "learning", "sincerely", "greenhouse", "recruiting",
})

CODE_WORD_RE = re.compile(r"(?i)\bcodes?\b")
URL_RE = re.compile(r"https?://[^\s<>\"'`\]\)\}]+", re.IGNORECASE)
LINK_PATH_HINT_RE = re.compile(r"(?i)(verify|confirm|activate|token)")
TAG_RE = re.compile(r"<[^>]+>")
DROP_BLOCK_RE = re.compile(r"(?is)<(script|style|head)\b.*?</\1\s*>")


# --------------------------------------------------------------------------------------
# Pure helpers (no Gmail; unit-tested directly)
# --------------------------------------------------------------------------------------

def registrable_domain(host: str) -> str:
    """Last two labels, or three when the suffix is a known two-level public suffix (co.uk)."""
    h = (host or "").strip().lower().rstrip(".")
    h = h.split(":", 1)[0]
    labels = [x for x in h.split(".") if x]
    if len(labels) <= 2:
        return ".".join(labels)
    if ".".join(labels[-2:]) in TWO_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _from_domain(from_header: str) -> tuple[str, str]:
    """(address, domain) from a From header; both lowercased."""
    _, addr = parseaddr(from_header or "")
    addr = addr.strip().lower()
    if "@" not in addr:
        m = re.search(r"[\w.+-]+@[\w.-]+", (from_header or "").lower())
        addr = m.group(0) if m else ""
    domain = addr.rsplit("@", 1)[1] if "@" in addr else ""
    return addr, domain


RESET_SUBJECT_RE = re.compile(r"(?i)(password reset|reset your password|reset password|forgot(ten)? password|new password)")
MONEY_RE = re.compile(r"(?i)\b(bank|chase|wells fargo|capital one|paypal|venmo|zelle|payment|invoice)\b")
# The account-security part of FORBIDDEN_RE, without the money words.
SECURITY_RE = re.compile(r"(?i)(password reset|reset your password|account recovery|recover your account|two-factor|2fa|"
                         r"sign-in attempt|new login)")


def is_forbidden(subject: str | None, snippet: str | None, *, allow_reset: bool = False, ats_sender: bool = False) -> bool:
    """Mail we never read, whatever a page asks: resets, recovery, money, 2FA, login alerts. The one
    exception: the portal's own password-reset email, read only inside fetch_password_reset_link after the
    agent itself asked that portal for a reset (allow_reset), and only for links on the portal's host.

    Links are not text: Q2's reset email was dropped because its link's tenant, 'q2ebanking', contains 'bank'
    (2026-10-20). Mail from the portal's own sender domain (ats_sender: otp.workday.com, greenhouse-mail.io, ...)
    is account mail, not money mail, even when the employer is a bank or a card company."""
    text = URL_RE.sub(" ", f"{subject or ''}\n{snippet or ''}")
    money = bool(MONEY_RE.search(text)) and not ats_sender
    if allow_reset and RESET_SUBJECT_RE.search(text) and not money:
        return False
    if ats_sender:
        return bool(SECURITY_RE.search(text))
    return bool(FORBIDDEN_RE.search(text))


def portal_domain_sender(from_header: str, portal: str | None) -> bool:
    """The message comes from one of the portal's own sender domains (otp.workday.com for Workday)."""
    spec = PORTALS.get((portal or "").lower())
    if not spec:
        return False
    _addr, domain = _from_domain(from_header)
    return bool(domain) and any(domain == d or domain.endswith("." + d) for d in spec["senders"] if "@" not in d)


GENERIC_LOCALS = {"noreply", "no-reply", "donotreply", "do-not-reply", "workday", "notifications", "notification", "mail",
                  "mailer", "careers", "recruiting", "jobs", "talent"}


def tenant_mismatch(from_header: str, portal_host: str) -> bool:
    """Workday sends every tenant's mail from the same domains with the tenant as the local part
    (brunswick@otp.workday.com). A message from ANOTHER tenant is someone else's code or link."""
    ph = (portal_host or "").lower()
    addr, _domain = _from_domain(from_header)
    if ph.endswith("oraclecloud.com"):
        # Oracle Recruiting Cloud: "<pod>.fa.sender@workflow.email....oraclecloud.com" for host "<pod>.fa.us2.oraclecloud.com".
        local = (addr or "").split("@")[0]
        return local.endswith(".fa.sender") and local.split(".")[0] != ph.split(".")[0]
    if not ph.endswith(("myworkdayjobs.com", "myworkday.com")):
        return False
    local = (addr or "").split("@")[0]
    if not local or local in GENERIC_LOCALS:
        return False
    return local != ph.split(".")[0]


_NAME_SUFFIXES = ("incorporated", "corporation", "company", "group", "inc", "corp", "llc", "ltd", "co")
_COMMON_FIRST = {"american", "national", "united", "general", "global", "international", "northern", "southern", "western",
                 "eastern", "central", "pacific", "atlantic", "federal", "capital", "advanced", "applied", "digital",
                 "standard", "universal", "consolidated", "integrated"}
_NAME_FILLER = {"the", "of", "and", "for", "a", "an"}


def company_sender_tokens(company: str) -> list[str]:
    """The employer's name as it appears in its own mail domain: 'American Express' -> 'americanexpress'
    (careers@recruitment.americanexpress.com sent Amex's Oracle access code, 2026-10-19), and the initials of a
    name of two or more words: Texas Instruments mails its Oracle code from recruiting.ti.com, and the run never
    found it (2026-10-20). A leading 'The' is no part of a domain ('The Toro Company' -> 'toro')."""
    words = [w for w in re.split(r"[^a-z0-9]+", (company or "").lower()) if w]
    while words and words[0] == "the":
        words = words[1:]
    if len(words) > 1 and words[-1] in _NAME_SUFFIXES:
        words = words[:-1]
    sq = "".join(words)
    for suffix in _NAME_SUFFIXES:
        if sq.endswith(suffix) and len(sq) - len(suffix) >= 3:
            sq = sq[: -len(suffix)]
            break
    out = [sq] if len(sq) >= 3 else []
    initials = "".join(w[0] for w in words if w not in _NAME_FILLER)
    if len([w for w in words if w not in _NAME_FILLER]) >= 2 and len(initials) >= 2 and initials not in out:
        out.append(initials)
    # A distinctive first word alone: Citizens Financial Group's Oracle code was never found in 30 minutes, the bank mails
    # as 'Citizens' (citizensbank.com) and neither 'citizensfinancial' nor 'cfg' matched (2026-10-21).
    if len(words) > 1 and len(words[0]) >= 6 and words[0] not in _COMMON_FIRST and words[0] not in out:
        out.append(words[0])
    return out


def workday_tenant_tokens(portal: str | None, portal_host: str) -> list[str]:
    """A Workday tenant that mails from its own domain (Invesco: 'Verify your candidate account' from
    Workday@invesco.com, 2026-10-20, never found because only Workday's domains were searched): the tenant name from
    the host ('invesco' in invesco.wd1.myworkdayjobs.com) as an extra sender."""
    ph = (portal_host or "").lower().strip()
    if (portal or "").lower() != "workday" and not ph.endswith(("myworkdayjobs.com", "myworkday.com")):
        return []
    tenant = ph.split(".")[0] if ph else ""
    if len(tenant) < 3 or re.fullmatch(r"wd\d+|www|careers|jobs|apply|myworkday|myworkdayjobs|workday", tenant):
        return []
    return [tenant]


# Career-site platforms that mail verification codes for their tenants from their own domain: Qualcomm's Eightfold site
# sent "Verify your email" from confirm@eightfold.ai, the search looked only for qualcomm.com, and the run waited 15 minutes
# for a code that was in the inbox (2026-10-21). Used for portals goat does not know by name; the time window (after the
# request) keeps another employer's code out.
PLATFORM_SENDERS = ("eightfold.ai", "phenompeople.com", "avature.net", "icims.com", "smartrecruiters.com", "myworkday.com",
                    "successfactors.com", "successfactors.eu", "sapsf.com", "oraclecloud.com", "taleo.net", "jobvite.com",
                    "greenhouse.io", "lever.co", "ashbyhq.com", "ultipro.com", "ukg.com")


def sender_allowed(from_header: str, portal: str | None, portal_host: str, extra: tuple[str, ...] | list[str] = ()) -> bool:
    """Known portal: From must be one of the portal's sender domains/addresses. Unknown portal:
    From domain must share the portal host's registrable domain. `extra`: the employer's own mail domain
    names (company_sender_tokens), for tenants that mail from the company rather than the portal."""
    addr, domain = _from_domain(from_header)
    if not domain:
        return False
    if extra and any(t and t in domain.split(".") for t in extra):
        return True
    # a long token also matches a whole word of the sender's display name ('Citizens Careers <...>'); never a domain that
    # merely starts with it (americanexpressoffers.example is no American Express)
    name = (from_header or "").split("<")[0].lower()
    if extra and any(t and len(t) >= 6 and re.search(r"\b" + re.escape(t) + r"\b", name) for t in extra):
        return True
    spec = PORTALS.get((portal or "").lower())
    if spec is None:
        rd = registrable_domain(portal_host)
        return (bool(rd) and registrable_domain(domain) == rd) or registrable_domain(domain) in PLATFORM_SENDERS
    if tenant_mismatch(from_header, portal_host):
        return False
    for s in spec["senders"]:
        s = s.lower()
        if s.endswith("@"):
            if (addr or "").startswith(s):
                return True
        elif "@" in s:
            if addr == s:
                return True
        elif domain == s or domain.endswith("." + s):
            return True
    return False


def strip_html(raw: str) -> str:
    raw = DROP_BLOCK_RE.sub(" ", raw or "")
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</td>|</tr>|</li>|</h\d>", "\n", raw)
    text = TAG_RE.sub(" ", raw)
    text = _html.unescape(text)
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def extract_code(text: str, portal: str) -> str | None:
    """Pick the verification code out of a message body. Prefers tokens near the word "code";
    for 8-alphanumeric portals prefers tokens mixing letters and digits and excludes words."""
    if not text:
        return None
    spec = PORTALS.get((portal or "").lower())
    pattern = spec["code_regex"] if spec else SIX_DIGITS
    body = URL_RE.sub(" ", text)  # URL segments are not codes
    code_positions = [m.start() for m in CODE_WORD_RE.finditer(body)]

    def distance(pos: int) -> int:
        if not code_positions:
            return 10 ** 6
        return min(abs(pos - c) for c in code_positions)

    candidates: list[tuple[int, int, int, str]] = []
    for m in re.finditer(pattern, body):
        tok = m.group(0)
        if tok.lower() in COMMON_WORDS:
            continue
        mixed = any(ch.isdigit() for ch in tok) and any(ch.isalpha() for ch in tok)
        rank = 0
        if pattern == EIGHT_ALNUM and not mixed:
            # a plain word or plain number only counts when it sits right next to "code"
            if distance(m.start()) > 40:
                continue
            rank = 1 if tok.isdigit() else 2
        candidates.append((rank, distance(m.start()), m.start(), tok))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][3]


def extract_links(text: str) -> list[str]:
    """http(s) links in order of appearance, de-duplicated, HTML entities decoded."""
    out: list[str] = []
    seen: set[str] = set()
    for m in URL_RE.finditer(text or ""):
        url = _html.unescape(m.group(0)).rstrip(".,;:!?\"'")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def link_host_ok(url: str, portal_host: str, portal: str | None = None) -> bool:
    """Shared ATS domain: the link host must equal the portal host or be a subdomain of it
    (a sibling tenant is someone else). Company domain: any host within its registrable domain."""
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    ph = (portal_host or "").lower().strip().rstrip(".")
    if not host or not ph:
        return False
    if host == ph or host.endswith("." + ph):
        return True
    rd = registrable_domain(ph)
    spec = PORTALS.get((portal or "").lower())
    if spec:
        rule = spec["link_host_rule"]
    else:
        rule = "same_host" if rd in SHARED_ATS_DOMAINS else "registrable"
    if rule == "registrable":
        return host == rd or host.endswith("." + rd)
    return False


def _company_tokens(company: str) -> list[str]:
    stop = {"inc", "llc", "ltd", "corp", "corporation", "co", "the", "and", "of", "company",
            "group", "holdings", "technologies", "technology", "labs", "plc", "gmbh"}
    raw = [t for t in re.split(r"[^A-Za-z0-9]+", company or "") if t]
    toks = [t for t in raw if t.lower() not in stop and len(t) >= 2]
    return toks or raw[:1]


# --------------------------------------------------------------------------------------
# Message model + client protocol
# --------------------------------------------------------------------------------------

@dataclass(repr=False)
class Message:
    """A parsed message. `text` and `raw` may contain the secret; never log or repr them."""
    id: str
    from_header: str
    subject: str
    snippet: str
    ts: int                 # epoch seconds (internalDate / 1000)
    text: str               # plain text + stripped HTML
    raw: str                # plain text + raw HTML (for href extraction)

    def __repr__(self) -> str:
        return f"Message(id={self.id!r}, ts={self.ts})"

    __str__ = __repr__


class MailClient(Protocol):
    def list_messages(self, q: str, max_results: int = 10) -> list[dict]: ...
    def get_message(self, msg_id: str) -> dict: ...


def _b64url(data: str | None) -> str:
    if not data:
        return ""
    pad = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + pad).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _walk_parts(payload: dict, plain: list[str], htmls: list[str]) -> None:
    mt = (payload.get("mimeType") or "").lower()
    data = (payload.get("body") or {}).get("data")
    if data:
        if mt.startswith("text/html"):
            htmls.append(_b64url(data))
        elif mt.startswith("text/") or not mt:
            plain.append(_b64url(data))
    for part in payload.get("parts") or []:
        _walk_parts(part, plain, htmls)


def parse_message(msg: dict) -> Message:
    payload = msg.get("payload") or {}
    headers = {(h.get("name") or "").lower(): h.get("value") or "" for h in payload.get("headers") or []}
    plain: list[str] = []
    htmls: list[str] = []
    _walk_parts(payload, plain, htmls)
    text = "\n".join(plain + [strip_html(h) for h in htmls])
    raw = "\n".join(plain + htmls)
    try:
        ts = int(int(msg.get("internalDate") or 0) / 1000)
    except (TypeError, ValueError):
        ts = 0
    return Message(
        id=str(msg.get("id") or ""),
        from_header=headers.get("from", ""),
        subject=headers.get("subject", ""),
        snippet=msg.get("snippet") or "",
        ts=ts,
        text=text,
        raw=raw,
    )


# --------------------------------------------------------------------------------------
# Gmail client (lazy; everything network-y becomes MailUnavailable)
# --------------------------------------------------------------------------------------

def _load_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    tok = Path(SETTINGS.mail.token_json)
    if not tok.exists():
        raise MailUnavailable(
            f"Gmail token not found at {tok}. Run the one-time authorization "
            f"(goat.mail.gmail.authorize_interactive) with the OAuth client file at "
            f"{SETTINGS.mail.credentials_json}."
        )
    try:
        creds = Credentials.from_authorized_user_file(str(tok), SCOPES)
    except Exception as e:  # malformed token file
        raise MailUnavailable(f"Gmail token at {tok} is unreadable ({type(e).__name__})") from None
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                raise MailUnavailable(f"Gmail token refresh failed ({type(e).__name__})") from None
            try:
                tok.write_text(creds.to_json())
            except OSError:
                log.warning("mail: could not persist refreshed token to %s", tok)
        else:
            raise MailUnavailable(
                "Gmail token is invalid and cannot be refreshed; re-run authorize_interactive()"
            )
    return creds


def _build_service():
    from googleapiclient.discovery import build

    creds = _load_credentials()
    try:
        # Every Gmail request gets a 30 s socket timeout: without one a stalled request hung Deutsche Bank's run for
        # 15 minutes with no event and no log line (2026-10-22).
        import google_auth_httplib2
        import httplib2
        http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=GMAIL_TIMEOUT_S))
        return build("gmail", "v1", http=http, cache_discovery=False)
    except Exception as e:
        raise MailUnavailable(f"Gmail service build failed ({type(e).__name__})") from None


GMAIL_TIMEOUT_S = 30


def authorize_interactive() -> str:
    """One-time: run the installed-app OAuth flow in a local browser and store the token.
    Returns the token path. The service runs unattended afterwards."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    cj = Path(SETTINGS.mail.credentials_json)
    tok = Path(SETTINGS.mail.token_json)
    if not cj.exists():
        raise MailUnavailable(
            f"Gmail OAuth client file not found at {cj}. Create a Desktop OAuth client in Google "
            f"Cloud Console (Gmail API enabled) and save its JSON there."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(cj), SCOPES)
    # A "Desktop app" client accepts any localhost port. A "Web application" client only accepts
    # redirect URIs registered in the console, so use a fixed port and tell the user what to register.
    import json as _json
    client_type = next(iter(_json.loads(cj.read_text(encoding="utf-8")).keys()), "installed")
    port = 0
    if client_type == "web":
        port = int(os.environ.get("GMAIL_OAUTH_PORT", "8765"))
        print(f"Web-application OAuth client detected: the console must list http://localhost:{port}/ under "
              f"'Authorized redirect URIs' for this client (or recreate it as a Desktop app).")
    creds = flow.run_local_server(port=port, open_browser=True)
    tok.parent.mkdir(parents=True, exist_ok=True)
    tok.write_text(creds.to_json())
    log.info("mail: stored Gmail token at %s", tok)
    return str(tok)


class GmailClient:
    """Thin wrapper over users().messages(). Built lazily on first use."""

    def __init__(self) -> None:
        self._service = None

    def _svc(self):
        if self._service is None:
            self._service = _build_service()
        return self._service

    RETRIES = 2

    @staticmethod
    def _transient(e: Exception) -> bool:
        """A dropped or stale connection (SSLError after an idle spell, reset, timeout), a 429/5xx answer, or a 403
        that is only a rate limit ("rateLimitExceeded", 2026-10-19: the reset-link poll failed on it outright)."""
        status = getattr(getattr(e, "resp", None), "status", None)
        if status is not None:
            try:
                s = int(status)
            except (TypeError, ValueError):
                return False
            if s == 403 and _RATE_LIMITED.search(str(e)):
                return True
            return s == 429 or s >= 500
        return True

    def _execute(self, make_request, what: str):
        """Run one API call; on a transient failure rebuild the service (a fresh connection: the cached one goes
        stale while idle and the next call fails with an SSLError, 2026-10-18) and try again.

        One call at a time across threads (_GMAIL_LOCK): the service and its httplib2 connection are not
        thread-safe. The password-reset poll and the confirmation-mail reconciler hit an SSLError together,
        both rebuilt and retried on the same connection, and the agent died in ntdll with an access
        violation (2026-10-19 02:54)."""
        last: Exception | None = None
        with _GMAIL_LOCK:
            for attempt in range(self.RETRIES + 1):
                try:
                    return make_request(self._svc()).execute()
                except MailUnavailable:
                    raise
                except Exception as e:  # noqa: BLE001 - classified below
                    last = e
                    if attempt == self.RETRIES or not self._transient(e):
                        break
                    limited = bool(_RATE_LIMITED.search(str(e)))
                    log.info("mail: Gmail %s failed (%s%s); retrying%s", what, type(e).__name__,
                             ", rate limited" if limited else "", "" if limited else " on a fresh connection")
                    if not limited:
                        self._service = None
                    time.sleep((3.0 * 2 ** attempt) if limited else 0.5 * (attempt + 1))
        raise MailUnavailable(f"Gmail {what} failed ({type(last).__name__})") from None

    def list_messages(self, q: str, max_results: int = 10) -> list[dict]:
        resp = self._execute(lambda s: s.users().messages().list(userId="me", q=q, maxResults=max_results), "list")
        return list(resp.get("messages") or [])

    def get_message(self, msg_id: str) -> dict:
        return self._execute(lambda s: s.users().messages().get(userId="me", id=msg_id, format="full"), "get")


_default_client: MailClient | None = None


def default_client() -> MailClient:
    global _default_client
    with _GMAIL_LOCK:
        if _default_client is None:
            _default_client = GmailClient()
        return _default_client


# --------------------------------------------------------------------------------------
# Query building + polling
# --------------------------------------------------------------------------------------

def _quote(term: str) -> str:
    return f'"{term}"' if " " in term else term


def build_code_query(portal: str, portal_host: str, after_ts: int, extra_senders: tuple[str, ...] | list[str] = ()) -> str:
    spec = PORTALS.get((portal or "").lower())
    if spec:
        senders = list(spec["senders"])
        hints = spec["subject_hints"]
    else:
        senders = [registrable_domain(portal_host)] + list(PLATFORM_SENDERS)
        hints = ["code", "Verify", "verification", "Confirm"]
    senders += [t for t in extra_senders if t and t not in senders]
    senders = [s[:-1] if s.endswith("@") else s for s in senders]
    q = f"from:({' OR '.join(senders)}) after:{int(after_ts)} newer_than:1d"
    if hints:
        q += " (" + " OR ".join(f"subject:{_quote(h)}" for h in hints) + ")"
    return q


def _poll_newest(
    client: MailClient,
    query: str,
    portal: str,
    portal_host: str,
    after_ts: int,
    timeout_s: float,
    poll_s: float,
    sleep: Callable[[float], None],
    accept: Callable[[Message], bool] | None = None,
    allow_reset: bool = False,
    extra_senders: tuple[str, ...] | list[str] = (),
) -> Message | None:
    """List/get every poll_s until a message passes the filters or timeout_s elapses.
    Returns the NEWEST acceptable message. Always makes at least one pass. `accept` is an extra
    filter (a verification email must carry a link on this portal's host)."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    seen: dict[str, Message | None] = {}
    listed_once = False
    while True:
        best: Message | None = None
        try:
            refs = client.list_messages(query, 10)
            listed_once = True
        except MailUnavailable as e:
            # Gmail unavailable for a moment (rate limit, dropped connection): keep polling until the deadline, and
            # report it only if no listing ever succeeded (then "not received" would be a guess).
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if listed_once:
                    return None
                raise
            log.info("mail: %s; polling again", e)
            sleep(min(max(poll_s, 5.0), remaining))
            continue
        for ref in refs:
            mid = str(ref.get("id") or "")
            if not mid:
                continue
            if mid not in seen:
                try:
                    m = parse_message(client.get_message(mid))
                except MailUnavailable as e:
                    log.info("mail: %s for message id=%s; trying it on the next pass", e, mid)
                    continue
                ats = portal_domain_sender(m.from_header, portal)
                allowed = sender_allowed(m.from_header, portal, portal_host, extra_senders)
                if not allowed and ats and accept is not None and tenant_mismatch(m.from_header, portal_host) and accept(m):
                    # a tenant that mails under its parent's name (Dow Jones as newscorp@otp.workday.com, 2026-10-20): the
                    # link on this very portal host decides
                    allowed = True
                ok = (
                    m.ts >= int(after_ts)
                    and not is_forbidden(m.subject, m.snippet, allow_reset=allow_reset, ats_sender=ats)
                    and allowed
                    and (accept is None or accept(m))
                )
                if not ok:
                    log.info("mail: skipped message id=%s (filtered)", mid)
                seen[mid] = m if ok else None
            m = seen[mid]
            if m is not None and (best is None or m.ts > best.ts):
                best = m
        if best is not None:
            log.info("mail: accepted message id=%s from=%s ts=%s",
                     best.id, _from_domain(best.from_header)[0], best.ts)
            return best
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        sleep(min(poll_s, remaining) if poll_s > 0 else remaining)


# --------------------------------------------------------------------------------------
# Public results
# --------------------------------------------------------------------------------------

class CodeHandle:
    """Carries a verification code without exposing it. The only way out is `enter`."""

    def __init__(self, code: str, message_id: str) -> None:
        self.__code = code
        self.message_id = message_id

    def enter(self, type_into: Callable[[str], None]) -> str:
        type_into(self.__code)
        return "entered"

    def length(self) -> int:
        return len(self.__code)

    def __repr__(self) -> str:
        return f"CodeHandle(len={len(self.__code)}, msg={self.message_id})"

    __str__ = __repr__

    def __reduce__(self):  # never pickled / deep-copied into records
        raise TypeError("CodeHandle cannot be serialized")


@dataclass(frozen=True)
class LinkResult:
    status: str                 # verified_link | host_mismatch | not_received
    url: str | None
    message_id: str | None


def fetch_code(
    portal: str,
    portal_host: str,
    after_ts: int,
    *,
    timeout_s: float | None = None,
    poll_s: float | None = None,
    client: MailClient | None = None,
    _sleep: Callable[[float], None] = time.sleep,
    company: str = "",
) -> CodeHandle | None:
    """Wait for the newest matching code email and return a CodeHandle, or None on timeout. `company`: the
    employer, whose own mail domain may send the code (Oracle tenants: Amex mails from recruitment.americanexpress.com)."""
    client = client or default_client()
    timeout_s = SETTINGS.budgets.code_wait_s if timeout_s is None else timeout_s
    poll_s = SETTINGS.mail.poll_interval_s if poll_s is None else poll_s
    extra = company_sender_tokens(company)
    query = build_code_query(portal, portal_host, after_ts, extra)
    msg = _poll_newest(client, query, portal, portal_host, after_ts, timeout_s, poll_s, _sleep, extra_senders=extra)
    if msg is None:
        log.info("mail: no code email for portal=%s host=%s within %ss", portal, portal_host, timeout_s)
        return None
    code = extract_code(msg.text, portal) or extract_code(msg.subject, portal)
    if not code:
        log.info("mail: message id=%s matched but no code pattern found", msg.id)
        return None
    return CodeHandle(code, msg.id)


def fetch_verification_link(
    portal: str,
    portal_host: str,
    after_ts: int,
    *,
    timeout_s: float | None = None,
    poll_s: float | None = None,
    client: MailClient | None = None,
    _sleep: Callable[[float], None] = time.sleep,
) -> LinkResult:
    """Wait for the newest matching email and return a verification link on the portal's host."""
    client = client or default_client()
    timeout_s = SETTINGS.budgets.code_wait_s if timeout_s is None else timeout_s
    poll_s = SETTINGS.mail.poll_interval_s if poll_s is None else poll_s
    extra = workday_tenant_tokens(portal, portal_host)
    query = build_code_query(portal, portal_host, after_ts, extra)

    def _has_portal_link(m: Message) -> bool:
        return any(link_host_ok(u, portal_host, portal) for u in (extract_links(m.raw) or extract_links(m.text)))

    # Keep waiting past emails for other tenants/hosts: the newest email is not always ours.
    msg = _poll_newest(client, query, portal, portal_host, after_ts, timeout_s, poll_s, _sleep, accept=_has_portal_link,
                       extra_senders=extra)
    if msg is None:
        other = _poll_newest(client, query, portal, portal_host, after_ts, 0, poll_s, _sleep, extra_senders=extra)
        return LinkResult("host_mismatch" if other is not None else "not_received", None, other.id if other else None)
    links = extract_links(msg.raw) or extract_links(msg.text)
    ok = [u for u in links if link_host_ok(u, portal_host, portal)]
    if not ok:
        log.info("mail: message id=%s had %d link(s), none on host %s", msg.id, len(links), portal_host)
        return LinkResult("host_mismatch", None, msg.id)
    preferred = [u for u in ok if LINK_PATH_HINT_RE.search(urlparse(u).path or "")]
    url = (preferred or ok)[0]
    log.info("mail: verification link from message id=%s host=%s", msg.id, urlparse(url).hostname)
    return LinkResult("verified_link", url, msg.id)


RESET_PATH_HINT_RE = re.compile(r"(?i)(reset|password|forgot|recover|token)")


def build_reset_query(portal: str, portal_host: str, after_ts: int, extra_senders: tuple[str, ...] | list[str] = ()) -> str:
    spec = PORTALS.get((portal or "").lower())
    senders = list(spec["senders"]) if spec else [registrable_domain(portal_host)]
    senders += [t for t in extra_senders if t and t not in senders]
    senders = [s[:-1] if s.endswith("@") else s for s in senders]
    q = f"from:({' OR '.join(senders)}) after:{int(after_ts)} newer_than:1d"
    return q + " (" + " OR ".join(f"subject:{_quote(h)}" for h in ("password", "reset", "Reset", "Forgot")) + ")"


def fetch_password_reset_link(
    portal: str,
    portal_host: str,
    after_ts: int,
    *,
    timeout_s: float | None = None,
    poll_s: float | None = None,
    client: MailClient | None = None,
    _sleep: Callable[[float], None] = time.sleep,
    company: str = "",
) -> LinkResult:
    """Wait for the portal's password-reset email and return its reset link (on the portal's host).
    Used when a tenant already holds an account under an older password: the agent asks for a reset and
    sets Surya's one application password on it. `company`: the employer, whose own mail domain may send it
    (PACCAR's SuccessFactors resets come from DoNotReply@paccar.com and were never found, 2026-10-22)."""
    client = client or default_client()
    timeout_s = SETTINGS.budgets.code_wait_s if timeout_s is None else timeout_s
    poll_s = SETTINGS.mail.poll_interval_s if poll_s is None else poll_s
    extra = workday_tenant_tokens(portal, portal_host)
    extra += [t for t in company_sender_tokens(company) if t not in extra]
    query = build_reset_query(portal, portal_host, after_ts, extra)

    def _has_portal_link(m: Message) -> bool:
        return any(link_host_ok(u, portal_host, portal) for u in (extract_links(m.raw) or extract_links(m.text)))

    msg = _poll_newest(client, query, portal, portal_host, after_ts, timeout_s, poll_s, _sleep, accept=_has_portal_link, allow_reset=True,
                       extra_senders=extra)
    if msg is None:
        return LinkResult("not_received", None, None)
    links = extract_links(msg.raw) or extract_links(msg.text)
    ok = [u for u in links if link_host_ok(u, portal_host, portal)]
    if not ok:
        return LinkResult("host_mismatch", None, msg.id)
    preferred = [u for u in ok if RESET_PATH_HINT_RE.search((urlparse(u).path or "") + "?" + (urlparse(u).query or ""))]
    url = (preferred or ok)[0]
    log.info("mail: password-reset link from message id=%s host=%s", msg.id, urlparse(url).hostname)
    return LinkResult("reset_link", url, msg.id)


def find_confirmation_email(
    company: str,
    after_ts: int,
    portal: str | None,
    *,
    client: MailClient | None = None,
) -> str | None:
    """Newest 'application received' email mentioning the company, or None."""
    client = client or default_client()
    toks = _company_tokens(company)
    if not toks:
        return None
    # Gmail ANDs bare words, and "ICF International"'s confirmation (from icf@myworkday.com) never says "International"
    # (2026-10-18): search for any of the name's words, then require all of them in the message, or the first one in the
    # sender ("ICF Human Resources <icf@myworkday.com>").
    company_q = "(" + " OR ".join(_quote(t) for t in toks) + ")" if len(toks) > 1 else _quote(toks[0])
    q = f'subject:(application OR applying OR received OR "thank you") after:{int(after_ts)} {company_q}'
    best: Message | None = None
    for ref in client.list_messages(q, 10):
        mid = str(ref.get("id") or "")
        if not mid:
            continue
        m = parse_message(client.get_message(mid))
        # a confirmation from the portal's own domain is account mail even from a bank ('Thank you for applying to
        # Capital One' from capitalone@myworkday.com)
        if m.ts < int(after_ts) or is_forbidden(m.subject, m.snippet, ats_sender=portal_domain_sender(m.from_header, portal)):
            continue
        hay = f"{m.from_header}\n{m.subject}\n{m.snippet}\n{m.text}".lower()
        if not (all(t.lower() in hay for t in toks) or toks[0].lower() in (m.from_header or "").lower()):
            continue
        if best is None or m.ts > best.ts:
            best = m
    if best is None:
        return None
    log.info("mail: confirmation candidate id=%s from=%s ts=%s portal=%s",
             best.id, _from_domain(best.from_header)[0], best.ts, portal)
    return best.id


__all__ = [
    "MailUnavailable", "PORTALS", "CodeHandle", "LinkResult", "MailClient", "GmailClient",
    "authorize_interactive", "fetch_code", "fetch_verification_link", "find_confirmation_email",
    "extract_code", "extract_links", "is_forbidden", "sender_allowed", "link_host_ok",
    "registrable_domain", "strip_html", "parse_message", "build_code_query",
]
