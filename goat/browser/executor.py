"""Playwright over CDP into the dedicated Chrome. Every click that comes from the model goes through
`click_xy`, which asks the page what sits under the cursor and refuses submit controls (outside
`submit()`) and anything inside a CAPTCHA frame. Screenshots are taken at a fixed viewport and
device scale factor 1 so a screenshot pixel is the pixel the click lands on."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from ..config import SETTINGS
from . import js

log = logging.getLogger(__name__)


class ClickRefused(RuntimeError):
    pass


class NavigationRefused(RuntimeError):
    pass


@dataclass
class ElementAt:
    found: bool
    tag: str = ""
    text: str = ""
    submit: bool = False
    captcha: bool = False
    why: str = ""
    ref: str = ""


# OpenAI computer-tool key names -> Playwright key names
_KEYMAP = {
    "ctrl": "Control", "control": "Control", "alt": "Alt", "option": "Alt", "shift": "Shift",
    "cmd": "Meta", "command": "Meta", "meta": "Meta", "super": "Meta", "win": "Meta",
    "enter": "Enter", "return": "Enter", "tab": "Tab", "esc": "Escape", "escape": "Escape",
    "backspace": "Backspace", "delete": "Delete", "del": "Delete", "space": " ",
    "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
    "arrowup": "ArrowUp", "arrowdown": "ArrowDown", "arrowleft": "ArrowLeft", "arrowright": "ArrowRight",
    "pageup": "PageUp", "pagedown": "PageDown", "home": "Home", "end": "End", "insert": "Insert",
    "capslock": "CapsLock", "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4", "f5": "F5", "f6": "F6",
    "f7": "F7", "f8": "F8", "f9": "F9", "f10": "F10", "f11": "F11", "f12": "F12",
}


def to_pw_key(k: str) -> str:
    kl = k.strip().lower()
    if kl in _KEYMAP:
        return _KEYMAP[kl]
    return k if len(k) > 1 else k


def url_host(url: str) -> str:
    """The bare host of a URL, lower-case, without port or credentials ("x.oraclecloud.com:443" -> "x.oraclecloud.com")."""
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


class HostPolicy:
    """Hosts the executor may navigate to for a job: the listing's host, hosts it redirects to
    (recorded as they happen on the application path), and known ATS hosts."""

    ATS_SUFFIXES = (
        "greenhouse.io", "ashbyhq.com", "lever.co", "myworkdayjobs.com", "myworkdaysite.com",
        "workday.com", "icims.com", "oraclecloud.com", "smartrecruiters.com", "successfactors.com",
        "sapsf.com", "eightfold.ai", "google.com", "gstatic.com", "recaptcha.net", "hcaptcha.com",
        "cloudflare.com",
    )

    def __init__(self, seed_urls: list[str]):
        self.hosts: set[str] = set()
        for u in seed_urls:
            h = url_host(u)
            if h:
                self.hosts.add(h)

    def allow(self, host: str) -> None:
        if host:
            self.hosts.add(host.lower().split(":")[0])

    def allowed(self, url: str) -> bool:
        h = url_host(url)
        if not h:
            return False
        if h in self.hosts:
            return True
        return any(h == s or h.endswith("." + s) for s in self.ATS_SUFFIXES)


class Executor:
    def __init__(self, cdp_url: str | None = None, policy: HostPolicy | None = None, job_id: int | None = None):
        self.cdp_url = cdp_url or SETTINGS.browser.cdp_url
        self.policy = policy or HostPolicy([])
        self.job_id = job_id
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self._cdp = None
        self.vw = SETTINGS.browser.viewport_w
        self.vh = SETTINGS.browser.viewport_h
        self.allow_submit_once = False
        self.action_log: list[dict] = []

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url, timeout=15000)
        if not self._browser.contexts:
            self._context = await self._browser.new_context()
        else:
            self._context = self._browser.contexts[0]

    async def start_launched(self, headless: bool = True) -> None:
        """Test mode: Playwright's own Chromium instead of the dedicated Chrome."""
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=headless)
        self._context = await self._browser.new_context(viewport={"width": self.vw, "height": self.vh},
                                                        device_scale_factor=1)

    async def close(self, close_browser: bool = False) -> None:
        try:
            if close_browser and self._browser:
                await self._browser.close()
            elif self._browser:
                # connect_over_cdp: closing the Browser object detaches without killing Chrome
                await self._browser.close()
        except Exception:
            pass
        if self._pw:
            await self._pw.stop()

    async def new_page(self) -> Page:
        assert self._context
        self.page = await self._context.new_page()
        await self._fix_viewport()
        return self.page

    async def attach(self, target_id: str | None = None, url_hint: str = "") -> Page | None:
        """Re-use a surviving tab by CDP target id or URL prefix."""
        assert self._context
        for p in self._context.pages:
            try:
                if target_id:
                    cdp = await self._context.new_cdp_session(p)
                    info = await cdp.send("Target.getTargetInfo")
                    await cdp.detach()
                    if info.get("targetInfo", {}).get("targetId") == target_id:
                        self.page = p
                        await self._fix_viewport()
                        return p
                elif url_hint and p.url.startswith(url_hint):
                    self.page = p
                    await self._fix_viewport()
                    return p
            except Exception:
                continue
        return None

    async def target_id(self) -> str | None:
        if not self.page or not self._context:
            return None
        try:
            cdp = await self._context.new_cdp_session(self.page)
            info = await cdp.send("Target.getTargetInfo")
            await cdp.detach()
            return info.get("targetInfo", {}).get("targetId")
        except Exception:
            return None

    async def _fix_viewport(self) -> None:
        assert self.page and self._context
        try:
            await self.page.set_viewport_size({"width": self.vw, "height": self.vh})
            cdp = await self._context.new_cdp_session(self.page)
            await cdp.send("Emulation.setDeviceMetricsOverride", {
                "width": self.vw, "height": self.vh, "deviceScaleFactor": 1, "mobile": False})
            self._cdp = cdp
        except Exception as e:  # pragma: no cover
            log.warning("viewport fix failed: %s", e)

    async def bring_to_front(self) -> None:
        if self.page:
            try:
                await self.page.bring_to_front()
            except Exception:
                pass

    # ------------------------------------------------------------------ navigation
    async def goto(self, url: str, wait: str = "domcontentloaded", timeout_ms: int = 45000) -> str:
        assert self.page
        if not self.policy.allowed(url):
            raise NavigationRefused(f"host not on allowlist: {urlparse(url).netloc}")
        await self.page.goto(url, wait_until=wait, timeout=timeout_ms)
        await self.settle()
        final = self.page.url
        self.policy.allow(url_host(final))  # redirects along the application path
        self._log("goto", {"url": url, "final": final})
        return final

    async def settle(self, quiet_ms: int = 600, timeout_ms: int = 8000) -> None:
        assert self.page
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
        try:
            await self.page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 4000))
        except Exception:
            pass
        await asyncio.sleep(quiet_ms / 1000)

    @property
    def url(self) -> str:
        return self.page.url if self.page else ""

    # ------------------------------------------------------------------ reading
    async def eval(self, script: str, arg: Any = None) -> Any:
        assert self.page
        res = await self.page.evaluate(script, arg)
        if isinstance(res, str):
            try:
                return json.loads(res)
            except (json.JSONDecodeError, TypeError):
                return res
        return res

    async def element_at(self, x: int, y: int) -> ElementAt:
        d = await self.eval(js.JS_ELEMENT_AT, [x, y])
        if not isinstance(d, dict):
            return ElementAt(found=False)
        return ElementAt(found=d.get("found", False), tag=d.get("tag", ""), text=d.get("text", ""),
                         submit=d.get("submit", False), captcha=d.get("captcha", False), why=d.get("why", ""),
                         ref=d.get("ref", ""))

    async def read_ref(self, ref: str) -> dict:
        d = await self.eval(js.JS_READ_REF, ref)
        return d if isinstance(d, dict) else {"found": False}

    async def page_text(self, max_chars: int = 20000) -> str:
        return await self.eval(js.JS_PAGE_TEXT, max_chars) or ""

    async def screenshot_png(self) -> bytes:
        assert self.page
        return await self.page.screenshot(type="png", full_page=False)

    async def screenshot_data_url(self) -> str:
        png = await self.screenshot_png()
        return "data:image/png;base64," + base64.b64encode(png).decode()

    async def zoom_png(self, x: int, y: int, w: int, h: int) -> bytes:
        assert self.page
        x = max(0, min(x, self.vw - 1)); y = max(0, min(y, self.vh - 1))
        w = max(10, min(w, self.vw - x)); h = max(10, min(h, self.vh - y))
        return await self.page.screenshot(type="png", clip={"x": x, "y": y, "width": w, "height": h}, scale="css")

    # ------------------------------------------------------------------ guarded actions
    async def _guard_point(self, x: int, y: int, allow_submit: bool) -> ElementAt:
        el = await self.element_at(x, y)
        if el.captcha:
            raise ClickRefused(f"refused: click target is inside a human-verification challenge ({el.why})")
        if el.submit and not allow_submit:
            raise ClickRefused(f"refused: click target is a submit control ({el.text or el.why}); only submit() may submit")
        return el

    async def click_xy(self, x: int, y: int, *, button: str = "left", count: int = 1, modifiers: list[str] | None = None,
                       allow_submit: bool = False) -> ElementAt:
        assert self.page
        el = await self._guard_point(x, y, allow_submit)
        mods = [to_pw_key(m) for m in (modifiers or []) if to_pw_key(m) in ("Control", "Alt", "Shift", "Meta")]
        for m in mods:
            await self.page.keyboard.down(m)
        try:
            await self.page.mouse.click(x, y, button=button, click_count=count)  # type: ignore[arg-type]
        finally:
            for m in reversed(mods):
                await self.page.keyboard.up(m)
        self._log("click", {"x": x, "y": y, "target": el.text[:60], "tag": el.tag})
        await asyncio.sleep(0.25)
        return el

    async def click_ref(self, ref: str, allow_submit: bool = False) -> dict:
        info = await self.read_ref(ref)
        if not info.get("found"):
            raise ClickRefused(f"ref {ref} not found on the page")
        await self.click_xy(int(info["x"]), int(info["y"]), allow_submit=allow_submit)
        return info

    async def move(self, x: int, y: int) -> None:
        assert self.page
        await self.page.mouse.move(x, y)

    async def drag(self, path: list[tuple[int, int]]) -> None:
        assert self.page
        if not path:
            return
        x0, y0 = path[0]
        await self._guard_point(x0, y0, False)
        await self.page.mouse.move(x0, y0)
        await self.page.mouse.down()
        for (x, y) in path[1:]:
            await self.page.mouse.move(x, y, steps=5)
        await self.page.mouse.up()
        self._log("drag", {"path": path[:4]})

    async def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        assert self.page
        await self.page.mouse.move(x, y)
        await self.page.mouse.wheel(dx, dy)
        await asyncio.sleep(0.3)
        self._log("scroll", {"x": x, "y": y, "dx": dx, "dy": dy})

    async def keypress(self, keys: list[str]) -> None:
        """Keys pressed together (a chord)."""
        assert self.page
        pw = [to_pw_key(k) for k in keys]
        combo = "+".join(pw)
        # Enter on a focused submit control would submit: check the active element first.
        if any(k == "Enter" for k in pw) and not self.allow_submit_once:
            active = await self.eval("""() => { const a = document.activeElement; if (!a) return ''; const t = a.tagName.toLowerCase();
                const type = (a.getAttribute('type')||'').toLowerCase(); const lbl = ((a.innerText||a.value||'')+'').trim();
                if ((t==='button'||t==='input') && (type==='submit' || /^(submit|submit application|apply|apply now|send application|finish|complete)$/i.test(lbl))) return 'submit:'+lbl;
                if (t==='input' && a.form && !/textarea/.test(t) && a.form.querySelector('button[type=submit],input[type=submit]') && !a.getAttribute('role')) return 'implicit-submit-risk';
                return ''; }""")
            if isinstance(active, str) and active.startswith("submit:"):
                raise ClickRefused(f"refused: Enter on a submit control ({active[7:]}); only submit() may submit")
            if active == "implicit-submit-risk":
                raise ClickRefused("refused: Enter inside a form text input can submit the form; use Tab or click instead")
        await self.page.keyboard.press(combo)
        self._log("keypress", {"keys": combo})

    async def type_text(self, text: str, *, real_keys: bool = False, delay_ms: int = 0) -> None:
        """Type into the focused element. `real_keys` sends key events per character, which
        autocomplete widgets (Workday prompts, Lever location) need."""
        assert self.page
        if real_keys:
            for ch in text:
                if ch == "\n":
                    raise ClickRefused("refused: newline in typed text could submit a form; use keypress Enter deliberately")
                await self.page.keyboard.press(ch if len(ch) == 1 else to_pw_key(ch))
                if delay_ms:
                    await asyncio.sleep(delay_ms / 1000)
        else:
            await self.page.keyboard.type(text, delay=delay_ms)
        self._log("type", {"len": len(text)})

    async def type_secret(self, text: str) -> None:
        """Same as type_text but never logged with content and never real-key echoed in records."""
        assert self.page
        await self.page.keyboard.type(text)
        self._log("type_secret", {"len": len(text)})

    async def focus_ref(self, ref: str) -> dict:
        info = await self.read_ref(ref)
        if not info.get("found"):
            raise ClickRefused(f"ref {ref} not found on the page")
        await self.page.mouse.click(int(info["x"]), int(info["y"]))  # focus by clicking (no submit risk on inputs)
        await asyncio.sleep(0.15)
        return info

    async def set_file(self, ref: str, path: str) -> dict:
        assert self.page
        loc = self.page.locator(f'[data-goat-ref="{ref}"]')
        if await loc.count() == 0:
            # inside a frame?
            for fr in self.page.frames:
                l2 = fr.locator(f'[data-goat-ref="{ref}"]')
                if await l2.count():
                    loc = l2
                    break
        await loc.first.set_input_files(path)
        await asyncio.sleep(0.8)
        return await self.read_ref(ref)

    async def wait(self, seconds: float = 1.0) -> None:
        await asyncio.sleep(min(seconds, 10.0))

    def _log(self, kind: str, detail: dict) -> None:
        self.action_log.append({"t": time.time(), "kind": kind, **detail})
        if len(self.action_log) > 500:
            self.action_log = self.action_log[-300:]
