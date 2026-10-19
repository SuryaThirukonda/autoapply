"""Configuration: read once at startup from .env + environment. Nothing here is re-read at
runtime (Hermes cached config silently; we log the hash we started with instead)."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = _env(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


@dataclass
class Models:
    executor: str = _env("EXECUTOR_MODEL", "gpt-6-luna")
    executor_effort: str = _env("EXECUTOR_EFFORT", "low")
    executor_retry_effort: str = _env("EXECUTOR_RETRY_EFFORT", "medium")
    advisor: str = _env("ADVISOR_MODEL", "gpt-6-luna")
    advisor_effort: str = _env("ADVISOR_EFFORT", "high")
    advisor_max_calls: int = _env_int("ADVISOR_MAX_CALLS", 3)
    classifier: str = _env("CLASSIFIER_MODEL", "gpt-6-luna")
    classifier_effort: str = _env("CLASSIFIER_EFFORT", "none")
    classifier_min_confidence: float = _env_float("CLASSIFIER_MIN_CONFIDENCE", 0.85)
    # The knowledge-base resolver (same model): reasons about what a question means before matching it.
    resolver_effort: str = _env("RESOLVER_EFFORT", "low")
    resolver_min_confidence_key: float = _env_float("RESOLVER_MIN_CONFIDENCE_KEY", 0.75)
    resolver_min_confidence_derived: float = _env_float("RESOLVER_MIN_CONFIDENCE_DERIVED", 0.85)
    service_tier: str = _env("OPENAI_SERVICE_TIER", "default")  # default | flex (auto-fallback)
    # Hard allowlist. Every request path calls `assert_allowed(model)` before sending anything.
    allowed: list = field(default_factory=lambda: [
        m.strip() for m in _env("ALLOWED_MODELS", "gpt-6-luna").split(",") if m.strip()
    ])


class ModelNotAllowed(RuntimeError):
    pass


def assert_allowed(model: str) -> str:
    allowed = SETTINGS.models.allowed
    if model not in allowed:
        raise ModelNotAllowed(f"model {model!r} is not in ALLOWED_MODELS={allowed}; refusing to send the request")
    return model


@dataclass
class Budgets:
    """Per-job budgets. A spent budget ends the run with a named reason; a retry moves up a tier."""
    max_failed_submits: int = _env_int("BUDGET_FAILED_SUBMITS", 2)
    max_steps: int = _env_int("BUDGET_STEPS", 0)          # 0 = no cap (Surya, 2026-10-18: never cut a run short on turns)
    max_pages: int = _env_int("BUDGET_PAGES", 0)          # 0 = no cap: a resumed Workday application re-walks every page
    wall_s_default: int = _env_int("BUDGET_WALL_S", 45 * 60)
    wall_s_by_portal: dict = field(default_factory=lambda: {
        "greenhouse": _env_int("BUDGET_WALL_S_GREENHOUSE", 15 * 60),
        "ashby": _env_int("BUDGET_WALL_S_ASHBY", 15 * 60),
        "lever": _env_int("BUDGET_WALL_S_LEVER", 15 * 60),
        "workday": _env_int("BUDGET_WALL_S_WORKDAY", 90 * 60),
        "oracle": _env_int("BUDGET_WALL_S_ORACLE", 90 * 60),
        "icims": _env_int("BUDGET_WALL_S_ICIMS", 60 * 60),
    })
    answer_wait_s: int = _env_int("ANSWER_WAIT_S", 15 * 60)
    # While Surya is away (nothing answered for AWAY_AFTER_S and a question already went unanswered in that time) a
    # new question waits only ANSWER_WAIT_AWAY_S, then the job holds; his answer, when it comes, requeues it.
    # 0 turns this off.
    answer_wait_away_s: int = _env_int("ANSWER_WAIT_AWAY_S", 120)
    away_after_s: int = _env_int("AWAY_AFTER_S", 2 * 3600)
    code_wait_s: int = _env_int("CODE_WAIT_S", 180)
    max_attempts: int = _env_int("BUDGET_MAX_ATTEMPTS", 3)
    max_held_tabs: int = _env_int("MAX_HELD_TABS", 4)


LINUX_CHROME_CANDIDATES = ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
                           "/usr/bin/chromium", "/usr/bin/chromium-browser")


def default_chrome_exe(platform: str = sys.platform, exists=os.path.exists) -> str:
    """CHROME_EXE when .env leaves it empty: Chrome stable on Windows and macOS; on Linux the first
    installed of Google Chrome or Chromium (Google ships no ARM build, so the ARM image has Chromium)."""
    if platform == "win32":
        return r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    if platform == "darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    for p in LINUX_CHROME_CANDIDATES:
        if exists(p):
            return p
    return LINUX_CHROME_CANDIDATES[0]


@dataclass
class Browser:
    chrome_exe: str = _env("CHROME_EXE", default_chrome_exe())
    user_data_dir: str = _env("CHROME_USER_DATA_DIR", str(ROOT / "chrome-profile"))
    cdp_port: int = _env_int("CDP_PORT", 9223)
    viewport_w: int = _env_int("VIEWPORT_W", 1440)
    viewport_h: int = _env_int("VIEWPORT_H", 900)
    screenshot_detail: str = _env("SCREENSHOT_DETAIL", "original")  # original | auto | "" (omit)
    launch_if_missing: bool = _env_bool("CHROME_LAUNCH_IF_MISSING", True)
    # Linux only: the Xvfb display Chrome draws on (scripts/entrypoint.sh starts it; noVNC shows it).
    display: str = _env("CHROME_DISPLAY", ":99")
    # Linux only: Docker's default seccomp profile blocks the namespaces Chrome's sandbox needs, so the
    # container sets this; the container is then the sandbox boundary.
    no_sandbox: bool = _env_bool("CHROME_NO_SANDBOX", False)

    @property
    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{self.cdp_port}"


@dataclass
class Human:
    # The bot token from @BotFather. TELEGRAM_KEY is the name the Settings tab writes; TELEGRAM_BOT_TOKEN still works.
    telegram_token: str = _env("TELEGRAM_KEY") or _env("TELEGRAM_BOT_TOKEN")
    telegram_owner_chat_id: int = _env_int("TELEGRAM_OWNER_CHAT_ID", 0)
    # The cloud browser in noVNC (the VM's Tailscale address, port 6080). Sent with every
    # ready_for_you notification so a CAPTCHA can be clicked from the phone. Empty on the PC.
    novnc_url: str = _env("NOVNC_URL")


@dataclass
class Mail:
    credentials_json: str = _env("GMAIL_CREDENTIALS_JSON", str(ROOT / "secrets" / "gmail_client.json"))
    token_json: str = _env("GMAIL_TOKEN_JSON", str(ROOT / "secrets" / "gmail_token.json"))
    address: str = _env("APPLICATION_EMAIL", "")
    poll_interval_s: float = _env_float("MAIL_POLL_S", 4.0)


@dataclass
class Creds:
    bw_exe: str = _env("BW_EXE", "bw")
    bw_session: str = _env("BW_SESSION")
    bw_passwordfile: str = _env("BW_PASSWORDFILE")  # path; used only to unlock, never logged
    # Unattended login (the cloud VM): a personal API key from the web vault. Never logged.
    bw_clientid: str = _env("BW_CLIENTID")
    bw_clientsecret: str = _env("BW_CLIENTSECRET")
    bw_server: str = _env("BW_SERVER")  # e.g. https://vault.bitwarden.eu; empty = bw's default
    application_email: str = _env("APPLICATION_EMAIL", "")
    # Surya's one application password, used for sign-in and sign-up on every portal whenever the Bitwarden
    # vault is not unlocked (Surya, 2026-10-18: "bitwarden cli is signed out, use PASSWORD_BIT"). Typed by the
    # harness only; never logged, never shown to the model.
    universal_password: str = _env("PASSWORD_BIT", "")
    # Surya uses one login on every Workday tenant; this vault item holds it. Sign-in AND sign-up on
    # any myworkdayjobs tenant reuse it instead of generating a per-tenant password.
    universal_item: str = _env("BW_UNIVERSAL_ITEM", "boeing.wd1.myworkdayjobs.com")


@dataclass
class Cloud:
    # The RDS Postgres URL for the goat_agent role (written by goat-dashboard's scripts/db-roles.mjs).
    # Set = the cloud link runs: this PC mirrors its state to RDS and takes commands from the website and
    # Telegram (which then runs on the website's server, not here). Empty = local only. Never logged.
    db_url: str = _env("GOAT_DB_URL")
    agent_name: str = _env("GOAT_AGENT_NAME", "cadoganpc")
    push_interval_s: float = _env_float("CLOUD_PUSH_INTERVAL_S", 2.0)
    command_poll_s: float = _env_float("CLOUD_COMMAND_POLL_S", 5.0)
    heartbeat_s: float = _env_float("CLOUD_HEARTBEAT_S", 15.0)
    screenshot_max_w: int = _env_int("CLOUD_SCREENSHOT_MAX_W", 1280)

    @property
    def enabled(self) -> bool:
        return bool(self.db_url)


@dataclass
class Settings:
    openai_api_key: str = _env("OPENAI_API_KEY")
    db_path: Path = Path(_env("GOAT_DB", str(ROOT / "data" / "goat.db")))
    records_dir: Path = Path(_env("RECORDS_DIR", str(ROOT / "records")))
    proposals_dir: Path = Path(_env("PROPOSALS_DIR", str(ROOT / "proposals")))
    resume_dir: Path = Path(_env("RESUME_DIR", str(ROOT / "resumes")))
    config_dir: Path = ROOT / "config"
    listings_url: str = _env(
        "LISTINGS_URL",
        "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
    )
    scrape_interval_s: int = _env_int("SCRAPE_INTERVAL_S", 15 * 60)
    # shadow = fill everything, run the gate, stop before submit. live = submit when the gate passes.
    mode: str = _env("GOAT_MODE", "shadow")
    live_portals: list = field(default_factory=lambda: [
        p.strip() for p in _env("LIVE_PORTALS", "").split(",") if p.strip()
    ])
    # Verified auto-submit (Surya 2026-10-18): "verified" submits a shadow draft at once when the gate, the read-back
    # on every page, the judge (with a fact check of every unconfirmed answer against the profile) and the question
    # log all agree; anything short of that holds as review_needed with a Submit button on Telegram. "off" keeps
    # the plain shadow hold.
    auto_submit: str = _env("AUTO_SUBMIT", "off")
    # Portals the worker never claims: iCIMS puts an hCaptcha in front of account creation (Surya 2026-10-18).
    skip_ats: list = field(default_factory=lambda: [
        p.strip().lower() for p in _env("SKIP_ATS", "icims").split(",") if p.strip()
    ])
    simplify_prefill: bool = _env_bool("SIMPLIFY_PREFILL", False)
    # Lanes the worker may claim. The account lane (Workday/Oracle/iCIMS) needs Bitwarden + Gmail;
    # enable it in Phase 3. Build order per the memo: no-account portals first.
    lanes_enabled: list = field(default_factory=lambda: [
        p.strip() for p in _env("LANES_ENABLED", "no_account,ready_for_you").split(",") if p.strip()
    ])
    safety_salt: str = _env("SAFETY_IDENTIFIER_SALT", "goat")
    log_level: str = _env("LOG_LEVEL", "INFO")
    dashboard_port: int = _env_int("DASHBOARD_PORT", 8787)
    # 127.0.0.1 on the PC. The container binds 0.0.0.0 and docker-compose publishes it on the VM's
    # loopback only; Tailscale carries it to Surya's devices.
    dashboard_host: str = _env("DASHBOARD_HOST", "127.0.0.1")
    # /healthz fails when the newest event is older than this (0 = report the age, never fail on it).
    health_max_event_age_s: int = _env_int("HEALTH_MAX_EVENT_AGE_S", 0)
    models: Models = field(default_factory=Models)
    budgets: Budgets = field(default_factory=Budgets)
    browser: Browser = field(default_factory=Browser)
    human: Human = field(default_factory=Human)
    mail: Mail = field(default_factory=Mail)
    creds: Creds = field(default_factory=Creds)
    cloud: Cloud = field(default_factory=Cloud)

    def portal_is_live(self, portal: str) -> bool:
        return self.mode == "live" and portal in self.live_portals

    def auto_submit_on(self) -> bool:
        return (self.auto_submit or "").strip().lower() == "verified"

    def wall_budget_s(self, portal: str) -> int:
        return self.budgets.wall_s_by_portal.get(portal, self.budgets.wall_s_default)

    def safety_identifier(self) -> str:
        return hashlib.sha256((self.safety_salt + self.mail.address).encode()).hexdigest()[:32]

    def config_hash(self) -> str:
        """Hash of the non-secret configuration, logged with every run."""
        d = asdict(self)
        for k in ("openai_api_key",):
            d.pop(k, None)
        d["human"].pop("telegram_token", None)
        d["creds"].pop("bw_session", None)
        d["creds"].pop("bw_passwordfile", None)
        d["creds"].pop("bw_clientid", None)
        d["creds"].pop("bw_clientsecret", None)
        d["cloud"].pop("db_url", None)
        blob = json.dumps(d, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:12]


SETTINGS = Settings()

for _p in (SETTINGS.db_path.parent, SETTINGS.records_dir, SETTINGS.proposals_dir):
    _p.mkdir(parents=True, exist_ok=True)

# Fail at import time, not mid-run, if the configured models are outside the allowlist.
for _m in (SETTINGS.models.executor, SETTINGS.models.advisor, SETTINGS.models.classifier):
    assert_allowed(_m)
