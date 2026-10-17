"""Launch and health-check the dedicated Chrome profile. Chrome 136+ refuses CDP on the default
profile, so we always pass our own --user-data-dir; the port is bound to 127.0.0.1 only."""
from __future__ import annotations

import asyncio
import logging
import subprocess
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
    "--window-size=1456,1000",
]


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


def launch() -> subprocess.Popen | None:
    b = SETTINGS.browser
    exe = Path(b.chrome_exe)
    if not exe.exists():
        log.error("chrome.exe not found at %s", exe)
        return None
    Path(b.user_data_dir).mkdir(parents=True, exist_ok=True)
    args = [
        str(exe),
        f"--remote-debugging-port={b.cdp_port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={b.user_data_dir}",
        *CHROME_FLAGS,
        "about:blank",
    ]
    log.info("launching Chrome on 127.0.0.1:%s with profile %s", b.cdp_port, b.user_data_dir)
    creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(args, creationflags=creationflags, close_fds=True)


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
