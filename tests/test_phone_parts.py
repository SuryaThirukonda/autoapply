"""Westinghouse (SuccessFactors, 2026-10-23): the form asks Home/Country Code, Home/Area Code and Home Phone # apart. His
candidate profile had prefilled his whole number into the area-code box, and it was taken as his answer because it
equals his stored phone. A box for one part of his number now takes only that part."""
import pytest

from goat import answers, db, phone
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures
from tests.test_gate_pages import _gate

PAGE = """<!doctype html><html><body style="margin:20px">
<form onsubmit="event.preventDefault();">
  <label for="cc">Home/Country Code</label><input id="cc" type="text" value="1"><br>
  <label for="ac">* Home/Area Code</label><input id="ac" type="text" required value="281-832-1205"><br>
  <label for="hp">* Home Phone #</label><input id="hp" type="text" required value="281-832-1205"><br>
  <label for="cp">Cell Phone #</label><input id="cp" type="text" value="281-832-1205"><br>
  <button type="submit">Apply</button>
</form></body></html>"""


def _phone():
    answers.save_answer("identity.phone", "281-832-1205", answer_type="text", source="seed:standing_answers.json")


def test_what_counts_as_a_part_box():
    assert phone.misfit("* Home/Area Code", "281-832-1205") and not phone.misfit("* Home/Area Code", "281")
    assert phone.misfit("Country code", "+1 281 832 1205") and not phone.misfit("Country code", "+1")
    assert phone.misfit("Home Phone #", "281-832-1205", local=True) and not phone.misfit("Home Phone #", "832-1205", local=True)
    assert not phone.misfit("Cell Phone #", "281-832-1205")
    for whole in ("Phone Number (including area code)", "Phone with country code", "* Mobile Phone Number", "Phone"):
        assert not phone.part_of(whole) and not phone.misfit(whole, "281-832-1205"), whole
    parts = [phone.part_of(x) for x in ("Area Code", "* Phone Country Code:", "Ext.", "Work Phone Extension")]
    assert parts == ["area", "country", "ext", "ext"]
    items = [{"ref": "e1", "tag": "input", "type": "text", "label": "Home/Country Code"},
             {"ref": "e2", "tag": "input", "type": "text", "label": "* Home/Area Code"},
             {"ref": "e3", "tag": "input", "type": "text", "label": "* Home Phone #"},
             {"ref": "e4", "tag": "input", "type": "text", "label": "Cell Phone #"}]
    assert [phone.follows_area_code(items, r) for r in ("e1", "e2", "e3", "e4")] == [False, False, True, False]


def test_the_parts_come_from_his_phone_number(store):
    _phone()
    assert phone.part_answer("area").answer == "281"
    local = phone.part_answer("local")
    assert local.answer == "832-1205" and local.source == "seed:derived" and "281-832-1205" in local.note


@pytest.mark.asyncio
async def test_the_gate_takes_his_number_only_where_the_whole_number_belongs(store, tmp_path):
    _phone()
    page = tmp_path / "ph.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        job = _job()
        snap = await snapshot.take(ex)
        g = _gate(job, snap, await ex.page_text(3000))
        blocked = " | ".join(g.reasons)
        assert "Home/Area Code = '281-832-1205'" in blocked and "Home Phone # = '281-832-1205'" in blocked, g.reasons
        assert "Cell Phone #" not in blocked
        # the park check sets each part from his number
        d, ctx = _dispatcher(job)
        ctx.ex = ex
        ctx.last_snapshot = snap
        out, _ = await d.t_park("needs_you: phone fields hold values with no sourced decision")
        vals = await ex.page.evaluate("() => ['cc','ac','hp','cp'].map(i => document.getElementById(i).value)")
        assert vals == ["1", "281", "832-1205", "281-832-1205"], (vals, out)
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_lookup_answers_each_part(store, tmp_path):
    _phone()
    page = tmp_path / "ph2.html"
    page.write_text(PAGE.replace('value="281-832-1205"', 'value=""'), encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = snap = await snapshot.take(ex)
        refs = {i["label"]: i["ref"] for i in snap.items if i.get("tag") == "input"}
        area, _ = await d.t_lookup_answer("Home/Area Code", [], None, refs["* Home/Area Code"])
        local, _ = await d.t_lookup_answer("Home Phone #", [], None, None)       # found by its label
        assert (area["key"], area["answer"]) == ("identity.phone_area_code", "281")
        assert (local["key"], local["answer"]) == ("identity.phone_local", "832-1205")
        cell, _ = await d.t_lookup_answer("Cell Phone #", [], None, refs["Cell Phone #"])
        assert cell.get("key") != "identity.phone_local"
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_his_phone_named_for_a_part_box_types_only_that_part(store, tmp_path):
    """Westinghouse (2026-10-23): the model filled 'Home Phone' after the area-code box from identity.phone, whole."""
    _phone()
    page = tmp_path / "ph3.html"
    page.write_text(PAGE.replace('value="281-832-1205"', 'value=""'), encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = snap = await snapshot.take(ex)
        refs = {i["label"]: i["ref"] for i in snap.items if i.get("tag") == "input"}
        out, _ = await d.t_fill_batch([{"ref": refs[lab], "answer_key": "identity.phone", "value": None, "source": None, "label": lab}
                                       for lab in ("* Home/Area Code", "* Home Phone #", "Cell Phone #")])
        assert [r["status"] for r in out["results"]] == ["set", "set", "set"], out
        vals = await ex.page.evaluate("() => ['ac','hp','cp'].map(i => document.getElementById(i).value)")
        assert vals == ["281", "832-1205", "281-832-1205"], vals
    finally:
        await ex.close(close_browser=True)


def test_an_extension_box_never_takes_his_number_and_clear_always_clears(store):
    """General Motors (Workday, 2026-10-24): 'Phone Extension' was sent identity.phone_local with source 'clear' and got
    '832-1205' typed into it; the judge failed the draft."""
    _phone()
    d, _ctx = _dispatcher(_job())
    assert d._resolve_value("identity.phone_local", None, "clear", "Phone Extension") == ("", "cleared", None)
    val, _src, err = d._resolve_value("identity.phone_local", None, "answer_key", "Phone Extension")
    assert val is None and "no extension" in err
    val, _src, err = d._resolve_value("identity.phone", None, "answer_key", "Phone Extension")
    assert val is None and err
    assert d._resolve_value("identity.phone", None, "answer_key", "Phone Number")[0] == "281-832-1205"
