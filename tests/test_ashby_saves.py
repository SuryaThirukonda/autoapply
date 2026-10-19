"""Ashby stores every answer with its own request the moment it changes, and two saves in flight at once lose one
(Sierra, 2026-10-19: four radios ticked in one script call, two never stored; the page showed all four and the
submit came back "Missing entry for required field")."""
import asyncio
import json

import pytest

from goat.browser import fill, js, snapshot
from goat.browser.executor import Executor, HostPolicy
from goat.browser.snapshot import Snapshot
from goat.gate import run_gate
from tests.test_answer_rules import _job, store  # noqa: F401 - fixtures

BASE = "https://jobs.ashbyhq.com/acme/1234/application"

# Shaped like Ashby's form: the screen shows each field's component state, `savedValue` comes from the server's form
# render (every save response carries the whole render), one save request per change, a text box saves 500 ms after
# typing stops. The fake React fibers carry the prop names Ashby's components use.
PAGE = """<!doctype html><html><body>
<div data-field-path="auth"><fieldset><label>Are you legally authorized to work in the United States?</label>
  <label><input type="radio" name="auth" value="Yes"> Yes</label><label><input type="radio" name="auth" value="No"> No</label></fieldset></div>
<div data-field-path="spon"><fieldset><label>Will you require sponsorship?</label>
  <label><input type="radio" name="spon" value="Yes"> Yes</label><label><input type="radio" name="spon" value="No"> No</label></fieldset></div>
<div data-field-path="hear"><fieldset><label>How did you hear about this opportunity?</label>
  <label><input type="radio" name="hear" value="LinkedIn"> LinkedIn</label><label><input type="radio" name="hear" value="Other"> Other</label></fieldset></div>
<div data-field-path="rec"><fieldset><label>Can we record interviews?</label>
  <label><input type="radio" name="rec" value="Yes"> Yes</label><label><input type="radio" name="rec" value="No"> No</label></fieldset></div>
<div data-field-path="name"><label for="nm">Preferred name</label><input id="nm" type="text"></div>
<button type="submit">Submit Application</button>
<script>
  const fields = {};
  const save = (path, value) => fetch('/save', { method: 'POST', body: JSON.stringify({ path, value }) })
    .then(r => r.json())
    .then(state => { for (const [p, f] of Object.entries(fields)) f.field.memoizedProps.savedValue = state[p]; });
  for (const entry of document.querySelectorAll('[data-field-path]')) {
    const path = entry.dataset.fieldPath;
    const field = { memoizedProps: { savedValue: undefined, field: { title: path, path } }, return: null, type: function TAe() {} };
    const group = { memoizedProps: { labeledValues: [], value: undefined }, return: field, type: function mOe() {} };
    fields[path] = { field };
    for (const input of entry.querySelectorAll('input')) {
      if (input.type === 'text') {
        const box = { memoizedProps: { value: '' }, return: field, type: function Pk() {} };
        input.__reactFiber$test = { memoizedProps: {}, return: box, type: 'input' };
        let timer = null;
        input.addEventListener('input', () => {
          box.memoizedProps.value = input.value;
          clearTimeout(timer);
          timer = setTimeout(() => save(path, input.value), 500);
        });
      } else {
        input.__reactFiber$test = { memoizedProps: {}, return: group, type: 'input' };
        input.addEventListener('change', () => { group.memoizedProps.value = input.value; save(path, input.value); });
      }
    }
  }
</script></body></html>"""


async def _form(state: dict, delay: float = 0.3):
    """A launched page at an Ashby URL whose save endpoint reads the stored form, waits, and writes it back whole,
    so a save that overlaps another loses one of them."""
    ex = Executor(policy=HostPolicy([BASE]))
    await ex.start_launched(headless=True)
    await ex.new_page()

    async def handler(route):
        if route.request.url.endswith("/save"):
            body = json.loads(route.request.post_data or "{}")
            seen = dict(state)
            await asyncio.sleep(delay)
            seen[body["path"]] = body["value"]
            state.clear()
            state.update(seen)
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(seen))
        else:
            await route.fulfill(status=200, content_type="text/html", body=PAGE)

    await ex._context.route("https://jobs.ashbyhq.com/**", handler)
    await ex.page.goto(BASE)
    await ex.settle(quiet_ms=100)
    return ex


async def _refs(ex) -> dict:
    await snapshot.take(ex)
    rows = await ex.eval("() => JSON.stringify(Array.from(document.querySelectorAll('input')).map(i => [i.name || i.id, i.value, i.getAttribute('data-goat-ref')]))")
    return {f"{n}:{v}" if v and n != "nm" else n: ref for n, v, ref in rows}


WANT = {"auth": "Yes", "spon": "No", "hear": "LinkedIn", "rec": "Yes"}


@pytest.mark.asyncio
async def test_fill_batch_on_ashby_waits_for_each_save(store):
    state: dict = {}
    ex = await _form(state)
    try:
        refs = await _refs(ex)
        fields = [{"ref": refs[f"{k}:{v}"], "value": True} for k, v in WANT.items()] + [{"ref": refs["nm"], "value": "Surya"}]
        out = await fill.fill_batch(ex, fields)
        assert all(r.ok for r in out), [r.as_dict() for r in out]
        assert state == {**WANT, "name": "Surya"}
        assert await ex.eval(js.JS_ASHBY_UNSAVED) == []
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_lost_saves_are_found_blocked_and_redone(store, monkeypatch):
    state: dict = {}
    ex = await _form(state)
    try:
        refs = await _refs(ex)
        # the old way: every radio ticked in one script call, so the saves overlap
        monkeypatch.setattr(fill, "AUTOSAVE_HOSTS", ())
        await fill.fill_batch(ex, [{"ref": refs[f"{k}:{v}"], "value": True} for k, v in WANT.items()])
        await asyncio.sleep(1.2)
        assert state != WANT                                   # the server kept only some of the four
        lost = await ex.eval(js.JS_ASHBY_UNSAVED)
        assert lost and {u["path"] for u in lost} == {k for k in WANT if k not in state}
        assert all(u["kind"] == "radio" for u in lost)

        # the snapshot names them and the gate refuses to submit
        monkeypatch.setattr(fill, "AUTOSAVE_HOSTS", ("ashbyhq.com",))
        snap = await snapshot.take(ex)
        assert snap.unsaved and "NOT STORED BY THE FORM" in snapshot.render(snap)
        job = _job()
        g = run_gate(job=job, snapshot=snap, page_text=await ex.page_text(5000), expected_resume=None, free_text_answers=[],
                     portal="ashby", page_no=1)
        assert any("has not stored it" in r for r in g.reasons), g.reasons

        # the re-save ticks each lost field away and back, one save at a time
        left = await fill.ashby_resave(ex)
        assert left == []
        assert state == WANT
        checked = await ex.eval("() => JSON.stringify(Array.from(document.querySelectorAll('input:checked')).map(i => i.name + ':' + i.value))")
        assert sorted(checked) == sorted(f"{k}:{v}" for k, v in WANT.items())
    finally:
        await ex.close(close_browser=True)


def test_an_unsaved_answer_blocks_the_gate(store):
    s = Snapshot(url=BASE, title="t", headings=[], items=[{"ref": "e1", "tag": "button", "label": "Submit Application", "submitish": True}],
                 groups=[], errors={}, challenge={})
    s.unsaved = [{"path": "auth", "title": "Are you legally authorized to work in the United States?", "shown": "Yes", "saved": "",
                  "kind": "radio", "refs": ["e2", "e3"]}]
    g = run_gate(job=_job(), snapshot=s, page_text="Submit Application", expected_resume=None, free_text_answers=[], portal="ashby")
    assert g.result != "pass_shadow" and g.result != "pass_live"
    assert any("legally authorized" in r and "has not stored it" in r for r in g.reasons)


def test_only_ashby_autosaves():
    class E:
        url = ""
    e = E()
    e.url = "https://jobs.ashbyhq.com/Sierra/x/application?embed=true"
    assert fill.autosaves(e)
    e.url = "https://job-boards.greenhouse.io/lyft/jobs/1"
    assert not fill.autosaves(e)
