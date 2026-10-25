"""Launch and health-check the dedicated Chrome profile. Chrome 136+ refuses CDP on the default
profile, so we always pass our own --user-data-dir; the port is bound to 127.0.0.1 only. On Linux
(the cloud VM) Chrome draws on the Xvfb display that scripts/entrypoint.sh starts, which noVNC shows."""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

import httpx

from ..config import SETTINGS

log = logging.getLogger(__name__)

# Flags from chrome-launcher's "flags for tools" list that keep background tabs alive, plus the
# ones that stop first-run UI. We do NOT disable extensions (Bitwarden stays for Surya's own use).
CHROME_FLAGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling",
    "--disable-session-crashed-bubble",
    "--hide-crash-restore-bubble",
    "--password-store=basic",
]
WINDOWS_FLAGS = ["--window-size=1456,1000"]
# Xvfb has no window manager or frame: the window is the whole 1440x900 screen noVNC shows.
LINUX_FLAGS = ["--window-size=1440,900", "--window-position=0,0", "--lang=en-US"]


async def cdp_version(cdp_url: str | None = None, timeout: float = 3.0) -> dict | None:
    url = (cdp_url or SETTINGS.browser.cdp_url).rstrip("/") + "/json/version"
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(url)
            if r.status_code == 200:
                return r.json()
    except Exception:
        return None
    return None


async def is_healthy() -> bool:
    return (await cdp_version()) is not None


async def page_answers(ws_url: str, timeout: float = 4.0) -> bool:
    """The tab runs a one-line script through its own DevTools endpoint. Only a timeout counts as hung; a refused or
    closed connection says nothing about the page."""
    import json

    import websockets

    try:
        async with websockets.connect(ws_url, open_timeout=timeout, max_size=None) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": "1", "returnByValue": True}}))

            async def reply() -> None:
                while True:
                    if json.loads(await ws.recv()).get("id") == 1:
                        return
            await asyncio.wait_for(reply(), timeout)
        return True
    except asyncio.TimeoutError:
        return False
    except Exception:  # noqa: BLE001
        return True


async def close_unresponsive_tabs(cdp_url: str | None = None, timeout: float = 4.0) -> list[dict]:
    """Close the tabs whose page no longer answers a DevTools call; return them ({id, url}).

    Playwright's connect_over_cdp attaches to every page and waits on each, so one hung tab makes every job fail
    'browser_down' while /json/version (all is_healthy checks) still answers: 2026-10-22 21:19-21:55, a CVS Workday
    tab and a SuccessFactors tab hung and the queue stood still for 35 minutes. A blank tab is opened first when every
    tab is hung, so Chrome does not quit with its last window."""
    import json

    import websockets

    base = (cdp_url or SETTINGS.browser.cdp_url).rstrip("/")
    ver = await cdp_version(base)
    if not ver:
        return []
    hung: list[dict] = []
    async with websockets.connect(ver["webSocketDebuggerUrl"], open_timeout=timeout, max_size=None) as ws:
        seq = 0

        async def call(method: str, params: dict | None = None, session: str | None = None) -> dict:
            nonlocal seq
            seq += 1
            mid = seq
            msg = {"id": mid, "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            await ws.send(json.dumps(msg))

            async def reply() -> dict:
                while True:
                    r = json.loads(await ws.recv())
                    if r.get("id") == mid:
                        return r
            return await asyncio.wait_for(reply(), timeout)

        pages = [t for t in (await call("Target.getTargets"))["result"]["targetInfos"] if t.get("type") == "page"]
        for t in pages:
            session = (await call("Target.attachToTarget", {"targetId": t["targetId"], "flatten": True}))["result"]["sessionId"]
            try:
                await call("Runtime.evaluate", {"expression": "1", "returnByValue": True}, session)
            except asyncio.TimeoutError:
                hung.append({"id": t["targetId"], "url": t.get("url", "")})
            try:
                await call("Target.detachFromTarget", {"sessionId": session})
            except Exception:  # noqa: BLE001 - a hung page may not answer the detach either
                pass
    # A tab can answer through the browser's flattened session and still not through its own endpoint, which Playwright
    # attaches by: CoStar's held Workday tab passed the check above while every connect_over_cdp timed out (2026-10-25)
    flagged = {t["id"] for t in hung}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            listed = (await c.get(f"{base}/json/list")).json()
    except Exception:  # noqa: BLE001 - the first pass stands alone
        listed = []
    rest = [t for t in listed if t.get("type") == "page" and t.get("id") not in flagged and t.get("webSocketDebuggerUrl")]
    answers = await asyncio.gather(*(page_answers(t["webSocketDebuggerUrl"], timeout) for t in rest))
    hung += [{"id": t["id"], "url": t.get("url", "")} for t, ok in zip(rest, answers) if not ok]
    if not hung:
        return []
    async with httpx.AsyncClient(timeout=10) as c:
        if len(hung) == len(pages):
            await c.put(f"{base}/json/new?about:blank")
        for t in hung:
            await c.get(f"{base}/json/close/{t['id']}")
    return hung


async def _list_targets(base: str | None = None) -> list[dict]:
    base = (base or SETTINGS.browser.cdp_url).rstrip("/")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{base}/json/list")
        return r.json() if r.status_code == 200 else []


async def _close_target(tid: str, base: str | None = None) -> None:
    base = (base or SETTINGS.browser.cdp_url).rstrip("/")
    async with httpx.AsyncClient(timeout=10) as c:
        await c.get(f"{base}/json/close/{tid}")


TAB_CAP = 20
LIVE_TAB_STATES = ("ready_for_you", "filling", "gated", "submitting", "awaiting_answer", "awaiting_code")


async def close_stale_tabs(cap: int = TAB_CAP) -> int:
    """Past `cap` open tabs, close every tab no live or ready-for-you job uses; returns how many were closed.

    connect_over_cdp attaches to every page: 42 tabs left by held and finished jobs made it outlast its 15 s timeout and
    every claim failed 'browser_down' (2026-10-24 10:11). A held job's tab is not needed: a rerun opens the apply link."""
    from .. import db
    pages = [t for t in await _list_targets() if t.get("type") == "page"]
    if len(pages) <= cap:
        return 0
    marks = ",".join("?" for _ in LIVE_TAB_STATES)
    keep = {r[0] for r in db.connect().execute(
        f"SELECT tab_target_id FROM jobs WHERE status IN ({marks}) AND tab_target_id IS NOT NULL", LIVE_TAB_STATES)}
    stale = [t for t in pages if t.get("id") not in keep]
    if len(stale) == len(pages):
        stale = stale[1:]              # Chrome quits with its last window: one tab stays
    for t in stale:
        try:
            await _close_target(t["id"])
        except Exception:  # noqa: BLE001 - a tab that will not close is tried on the next pass
            pass
    return len(stale)


def launch_command(b=None, platform: str = sys.platform) -> tuple[list[str], dict[str, str] | None]:
    """argv and environment (None = inherit) for the dedicated Chrome on this platform."""
    b = b or SETTINGS.browser
    args = [
        str(b.chrome_exe),
        f"--remote-debugging-port={b.cdp_port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={b.user_data_dir}",
        *CHROME_FLAGS,
    ]
    if platform == "win32":
        return [*args, *WINDOWS_FLAGS, "about:blank"], None
    args += LINUX_FLAGS if platform.startswith("linux") else []
    if b.no_sandbox:
        args.append("--no-sandbox")
    env = dict(os.environ)
    if platform.startswith("linux"):
        env["DISPLAY"] = b.display
        env.setdefault("LANG", "en_US.UTF-8")
        env["LANGUAGE"] = "en_US:en"
    return [*args, "about:blank"], env


def launch() -> subprocess.Popen | None:
    b = SETTINGS.browser
    exe = Path(b.chrome_exe)
    if not exe.exists():
        log.error("Chrome not found at %s (set CHROME_EXE)", exe)
        return None
    Path(b.user_data_dir).mkdir(parents=True, exist_ok=True)
    args, env = launch_command(b)
    log.info("launching Chrome on 127.0.0.1:%s with profile %s%s", b.cdp_port, b.user_data_dir,
             f" on display {env['DISPLAY']}" if env and "DISPLAY" in env else "")
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return subprocess.Popen(args, creationflags=creationflags, close_fds=True)
    # Own session so a Ctrl+C aimed at goat does not take Chrome (and the held tabs) with it. Chrome is
    # chatty on Xvfb (dbus, GPU); its output goes to data/chrome.log, not the container log.
    with open(SETTINGS.db_path.parent / "chrome.log", "ab") as out:
        return subprocess.Popen(args, env=env, start_new_session=True, close_fds=True,
                                stdout=subprocess.DEVNULL, stderr=out)


async def ensure_running(wait_s: float = 25.0) -> bool:
    """Return True when CDP answers. Launches Chrome if allowed and nothing is listening."""
    if await is_healthy():
        return True
    if not SETTINGS.browser.launch_if_missing:
        return False
    launch()
    deadline = asyncio.get_event_loop().time() + wait_s
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(1.0)
        if await is_healthy():
            return True
    return False
