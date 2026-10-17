"""Probe Workday's Sign In / Create Account behaviour on the live Assurant tab, using the Bitwarden helper
for the password (never printed). Usage: python scripts/workday_signin_probe.py [login|create]"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from goat.browser.executor import Executor, HostPolicy  # noqa: E402
from goat.browser import js, snapshot  # noqa: E402
from goat.creds import bitwarden  # noqa: E402
from goat import records  # noqa: E402

HOST = "assurant.wd1.myworkdayjobs.com"
EMAIL = __import__("goat.config", fromlist=["SETTINGS"]).SETTINGS.creds.application_email


async def main(mode: str) -> None:
    ex = Executor(policy=HostPolicy([f"https://{HOST}/"]), job_id=5)
    await ex.start()
    p = await ex.attach(url_hint=f"https://{HOST}/")
    print("tab:", ex.url if p else None)
    if not p:
        return
    await ex.bring_to_front()
    s = await snapshot.take(ex)

    def find(label_part, typ=None):
        for i in s.items:
            if label_part.lower() in (i.get("label") or "").lower() and (typ is None or i.get("type") == typ or i.get("tag") == typ):
                return i["ref"]
        return None

    if mode in ("login", "login_universal"):
        # make sure the Sign In form is showing
        btn = find("Sign In", "button")
        if find("Verify New Password") and btn:
            await ex.click_ref(btn); await ex.settle(); s = await snapshot.take(ex)
        email_ref, pw_ref = find("Email Address", "text"), find("Password", "password")
        click_label = "Sign In"
    else:
        btn = find("Create Account", "button")
        if not find("Verify New Password") and btn:
            await ex.click_ref(btn); await ex.settle(); s = await snapshot.take(ex)
        email_ref, pw_ref = find("Email Address", "text"), find("Password*", "password")
        confirm_ref = find("Verify New Password", "password")
        click_label = "Create Account"
    print("refs:", email_ref, pw_ref)
    loop = asyncio.get_event_loop()

    async def type_into(ref, text, secret):
        await ex.focus_ref(ref)
        await ex.page.keyboard.press("Control+a"); await ex.page.keyboard.press("Backspace")
        if secret:
            await ex.type_secret(text)
        else:
            await ex.type_text(text)

    def t_email(v): asyncio.run_coroutine_threadsafe(type_into(email_ref, v, False), loop).result(30)
    def t_pw(v): asyncio.run_coroutine_threadsafe(type_into(pw_ref, v, True), loop).result(30)
    status = await asyncio.to_thread(bitwarden.fill_credentials, HOST, EMAIL, t_email, t_pw, mode)
    print("credentials:", status)
    if mode == "create":
        def t_pw2(v): asyncio.run_coroutine_threadsafe(type_into(confirm_ref, v, True), loop).result(30)
        await asyncio.to_thread(bitwarden.fill_confirm_password, HOST, t_pw2)
    s2 = await snapshot.take(ex)
    print("before click, errors:", json.dumps({k: v for k, v in s2.errors.items() if k != 'url'})[:300])
    btn = next(i["ref"] for i in s2.items if (i.get("label") or "").strip().lower() == click_label.lower() and i.get("tag") == "button")
    before_url = ex.url
    await ex.click_ref(btn)
    for _ in range(12):
        await asyncio.sleep(1.0)
    print("after click url:", ex.url, "(changed)" if ex.url != before_url else "(same)")
    errs = await ex.eval(js.JS_PAGE_ERRORS)
    print("alerts:", [a.get("text") for a in errs["alerts"]], "error_text:", [e.get("text") for e in errs["error_text"]])
    print("text:", (await ex.page_text(1200)).replace("\n", " | ")[:700])
    print("screenshot:", records.save_screenshot(5, await ex.screenshot_png(), f"probe_{mode}"))
    await ex.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "login"))
