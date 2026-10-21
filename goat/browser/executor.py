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
        # Tabs this job opened (its own page plus popups / target=_blank tabs), newest last. Other jobs'
        # held tabs share the browser context and are never listed or touched here.
        self.job_pages: list[Page] = []
        self._new_tabs: list[Page] = []
        self._tab_notes: list[str] = []
        self.pending_chooser = None   # a native file picker the page opened (intercepted, never shown)
        # fetch / XHR requests in flight on this job's tabs (start time by request), for wait_saves
        self._inflight: dict = {}
        self._net_t = 0.0

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
        self._track(self.page)
        await self._fix_viewport()
        return self.page

    # ------------------------------------------------------------------ tabs
    def _track(self, page: Page) -> None:
        if page in self.job_pages:
            return
        self.job_pages.append(page)
        page.on("popup", self._on_popup)
        # With a listener attached Chrome never shows the OS file dialog (the agent could not see or
        # close it); upload_resume fulfils the pending chooser instead.
        page.on("filechooser", self._on_filechooser)
        page.on("close", lambda p=page: self.job_pages.remove(p) if p in self.job_pages else None)
        page.on("request", self._on_request)
        page.on("requestfinished", self._on_request_done)
        page.on("requestfailed", self._on_request_done)

    def _on_request(self, req) -> None:
        try:
            if req.resource_type in ("fetch", "xhr"):
                self._inflight[req] = time.monotonic()
                self._net_t = time.monotonic()
        except Exception:
            pass

    def _on_request_done(self, req) -> None:
        if self._inflight.pop(req, None) is not None:
            self._net_t = time.monotonic()

    async def wait_saves(self, min_s: float = 0.7, quiet_s: float = 0.3, timeout_s: float = 6.0) -> bool:
        """Wait for a form's background saves: at least min_s, then until no fetch / XHR is in flight and none started
        or finished for quiet_s. Ashby stores every answer with its own request as it changes (a text box after a
        500 ms pause), and overlapping saves lose answers (Sierra, 2026-10-19). A request older than 10 s (a poll
        or stream) is not waited for. True when the page went quiet before timeout_s."""
        t0 = time.monotonic()
        await asyncio.sleep(min_s)
        while time.monotonic() - t0 < timeout_s:
            now = time.monotonic()
            live = [t for t in self._inflight.values() if now - t < 10.0]
            if not live and now - self._net_t >= quiet_s:
                return True
            await asyncio.sleep(0.05)
        return False

    def _on_popup(self, popup: Page) -> None:
        self._track(popup)
        self._new_tabs.append(popup)

    def _on_filechooser(self, chooser) -> None:
        self.pending_chooser = chooser
        self._tab_notes.append("The page opened a file picker. Call upload_resume to attach the file "
                               "(the picker is intercepted; no system dialog is shown).")

    async def follow_new_tab(self) -> str:
        """After an action: if it opened a tab, move to the newest one when its host is allowed, else close
        it. Returns a note for the model ("" when nothing happened)."""
        notes: list[str] = []
        while self._new_tabs:
            tab = self._new_tabs.pop(0)
            try:
                await tab.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            url = tab.url or ""
            if url in ("", "about:blank"):
                try:  # window.open() then navigate: give it a moment
                    await tab.wait_for_url(lambda u: u not in ("", "about:blank"), timeout=5000)
                    url = tab.url
                except Exception:
                    pass
            if self.policy.allowed(url):
                self.page = tab
                await self._fix_viewport()
                await self.bring_to_front()
                await self.settle(quiet_ms=300, timeout_ms=5000)
                self._log("tab_follow", {"url": url[:200]})
                notes.append(f"That action opened a new tab ({url[:160]}); you are now in it. "
                             "Use the tabs tool to list tabs or switch back.")
            else:
                self._log("tab_closed_offsite", {"url": url[:200]})
                try:
                    await tab.close()
                except Exception:
                    pass
                notes.append(f"That action opened a tab outside this application ({url_host(url) or url[:80]}); "
                             "it was closed and you are still on the form.")
        notes += self._tab_notes
        self._tab_notes = []
        return " ".join(notes)

    async def list_tabs(self) -> list[dict]:
        out = []
        for i, p in enumerate(self.job_pages):
            try:
                title = await p.title()
            except Exception:
                title = ""
            out.append({"index": i, "url": p.url[:200], "title": title[:120], "current": p is self.page})
        return out

    async def switch_tab(self, index: int) -> dict:
        if not 0 <= index < len(self.job_pages):
            raise NavigationRefused(f"no tab {index}; call tabs with action 'list'")
        tab = self.job_pages[index]
        if not self.policy.allowed(tab.url):
            raise NavigationRefused(f"tab {index} is outside this application ({url_host(tab.url)})")
        self.page = tab
        await self._fix_viewport()
        await self.bring_to_front()
        self._log("tab_switch", {"index": index})
        return {"index": index, "url": tab.url[:200]}

    async def close_tab(self, index: int) -> dict:
        if not 0 <= index < len(self.job_pages):
            raise NavigationRefused(f"no tab {index}; call tabs with action 'list'")
        if len(self.job_pages) == 1:
            raise NavigationRefused("that is the only tab of this application")
        tab = self.job_pages[index]
        await tab.close()
        if tab is self.page:
            self.page = self.job_pages[-1]
            await self._fix_viewport()
            await self.bring_to_front()
        self._log("tab_close", {"index": index})
        return {"closed": index, "now": self.page.url[:200] if self.page else ""}

    async def close_other_tabs(self) -> None:
        """End of a run: keep only the tab the job is on."""
        for p in list(self.job_pages):
            if p is not self.page:
                try:
                    await p.close()
                except Exception:
                    pass

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
                        self._track(p)
                        await self._fix_viewport()
                        return p
                elif url_hint and p.url.startswith(url_hint):
                    self.page = p
                    self._track(p)
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
        try:
            return await self.page.screenshot(type="png", full_page=False, timeout=20000)
        except Exception as first:
            # Playwright waits for web fonts before it captures; a busy page stalled that past 30 s and the exception
            # ended Walmart's run (2026-10-20). Chrome's own capture does not wait for fonts.
            try:
                cdp = await self.page.context.new_cdp_session(self.page)
                try:
                    res = await cdp.send("Page.captureScreenshot", {"format": "png"})
                finally:
                    try:
                        await cdp.detach()
                    except Exception:
                        pass
                return base64.b64decode(res["data"])
            except Exception:
                raise first

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

    async def _hit(self, ref: str, x: int, y: int) -> str:
        try:
            out = await self.eval(js.JS_HIT_TARGET, [ref, x, y])
        except Exception:
            return "unknown"
        return out if isinstance(out, str) else "unknown"

    async def click_ref(self, ref: str, allow_submit: bool = False) -> dict:
        info = await self.read_ref(ref)
        if not info.get("found"):
            raise ClickRefused(f"ref {ref} not found on the page")
        x, y = int(info["x"]), int(info["y"])
        hit = await self._hit(ref, x, y)
        if hit.startswith("covered"):
            # Something sits over the target. Bitwarden's inline menu ("Unlock your account to view autofill suggestions")
            # opens under a focused email or password box and covered Workday's Reset Password and Create Account
            # buttons: the click landed on the menu, so no reset email was ever sent (Northwestern Mutual, Analog
            # Devices, 2026-10-20). Leaving the box closes the menu; Escape closes most others.
            await self.eval("() => { const a = document.activeElement; if (a && a !== document.body && a.blur) a.blur(); return true; }")
            await asyncio.sleep(0.35)
            info2 = await self.read_ref(ref)
            if info2.get("found"):
                info, x, y = info2, int(info2["x"]), int(info2["y"])
            hit = await self._hit(ref, x, y)
        if hit.startswith("covered"):
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(0.35)
            hit = await self._hit(ref, x, y)
        if hit.startswith("covered"):
            g = await self.eval(js.JS_TARGET_GUARD, ref)
            g = g if isinstance(g, dict) else {}
            if g.get("captcha"):
                raise ClickRefused(f"refused: click target is inside a human-verification challenge ({g.get('why')})")
            if g.get("submit") and not allow_submit:
                raise ClickRefused(f"refused: click target is a submit control ({g.get('text') or g.get('why')}); only submit() may submit")
            # findRef looks inside shadow roots too: a plain document query found nothing for SmartRecruiters' Next and the
            # click was reported done without happening (ServiceNow, 2026-10-21).
            if not await self.eval(js.JS_CLICK_REF, ref):
                raise ClickRefused(f"ref {ref} is covered by {hit[8:60] or 'another element'} and could not be clicked directly")
            self._log("click", {"ref": ref, "direct": True, "covered_by": hit[8:60]})
            await asyncio.sleep(0.25)
            return {**info, "covered_by": hit[8:60]}
        await self.click_xy(x, y, allow_submit=allow_submit)
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
        # Texas Instruments' cookie wall lay over its six Oracle code boxes (2026-10-20): the focusing click landed on
        # the wall, focus stayed on <body> and every digit of the code was lost. A covered field is focused directly
        # (a click there could press one of the covering layer's buttons); a clicked field that did not take the focus
        # is focused directly too.
        hit = await self._hit(ref, int(info["x"]), int(info["y"]))
        if not hit.startswith("covered"):
            await self.page.mouse.click(int(info["x"]), int(info["y"]))  # focus by clicking (no submit risk on inputs)
            await asyncio.sleep(0.15)
        try:
            await self.eval(js.JS_FOCUS_REF, ref)
        except Exception:
            pass
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
        try:
            await loc.first.set_input_files(path)
        except Exception as e:
            if "not an HTMLInputElement" not in str(e):
                raise
            # A 'Select files' button was named instead of its file input (Auto-Owners' Workday transcript slot,
            # 2026-10-20): use the file input in the same field or drop zone.
            box = loc.first.locator("xpath=ancestor::*[.//input[@type='file']][1]")
            if await box.count():
                inp = box.locator("input[type=file]").first
                await inp.set_input_files(path)
            else:
                # Google Careers (2026-10-21): the Upload button opens the browser's file chooser; no file input exists
                # in the page until then, and the search above waited 30 s for one that never came.
                async with self.page.expect_file_chooser(timeout=8000) as chooser:
                    await loc.first.click()
                await (await chooser.value).set_files(path)
        await asyncio.sleep(0.8)
        return await self.read_ref(ref)

    async def wait(self, seconds: float = 1.0) -> None:
        await asyncio.sleep(min(seconds, 10.0))

    def _log(self, kind: str, detail: dict) -> None:
        self.action_log.append({"t": time.time(), "kind": kind, **detail})
        if len(self.action_log) > 500:
            self.action_log = self.action_log[-300:]
