"""Hex (Ashby, 2026-10-23): 'Graduation Year' offered Summer 2027, Winter 2027, Summer 2028, Winter 2028, Summer 2029; the
bare stored year '2028' ticked 'Winter 2028' (the first label holding 2028) although he graduates in May 2028."""
import pytest

from goat import answers
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

TERMS = ["Summer 2027", "Winter 2027", "Winter 2028", "Summer 2028", "Summer 2029"]
PAGE = ("""<!doctype html><html><body style="margin:20px"><form><fieldset><legend>Graduation Year</legend>"""
        + "".join(f'<label><input type="radio" name="g" id="t{i}"> {t}</label>' for i, t in enumerate(TERMS))
        + "</fieldset></form></body></html>")


@pytest.mark.asyncio
async def test_a_bare_graduation_year_takes_the_term_nearest_his_graduation(store, tmp_path):
    answers.save_answer("education.graduation_year", "2028", answer_type="text", source="seed:standing_answers.json")
    answers.save_answer("education.graduation", "May 2028", answer_type="text", source="seed:standing_answers.json")
    page = tmp_path / "hex.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = snap = await snapshot.take(ex)
        refs = {o["label"].strip(): o["ref"] for g in snap.groups for o in g["options"]}
        out, _ = await d.t_fill_batch([{"ref": refs["Winter 2028"], "answer_key": "education.graduation_year", "value": None,
                                        "source": None, "label": "Graduation Year"}])
        r = out["results"][0]
        assert r["status"] == "failed" and "Summer 2028" in r["error"], r
        assert not await ex.page.is_checked("#t2")
        out, _ = await d.t_fill_batch([{"ref": refs["Summer 2028"], "answer_key": "education.graduation_year", "value": None,
                                        "source": None, "label": "Graduation Year"}])
        assert out["results"][0]["status"] == "set", out
        assert await ex.page.is_checked("#t3")
    finally:
        await ex.close(close_browser=True)
