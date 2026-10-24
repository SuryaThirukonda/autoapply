"""Dell (Oracle, 2026-10-24): Postal Code and City cx-select pickers held 77479 and Sugar Land (aria-invalid false, the
'Remove value' button shown), but read_ref took an input with aria-haspopup for a dropdown button and read its text: ''.
The pick was refused and the run handed the page to Surya."""
import pytest

from goat import answers
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

PAGE = """<!doctype html><html><body style="margin:20px"><form>
<label for="postalCode-71">Postal Code *</label>
<div class="cx-select-container"><input name="postalCode" id="postalCode-71" type="text" role="combobox" aria-autocomplete="list"
 aria-haspopup="grid" aria-expanded="false" aria-invalid="false" aria-required="true" class="cx-select-input" value="77479">
<button type="button" aria-label="Remove value for the Postal Code field." class="icon-clear"></button></div>
</form></body></html>"""


@pytest.mark.asyncio
async def test_an_oracle_picker_reads_back_its_own_value(store, tmp_path):
    answers.save_answer("identity.zip", "77479", answer_type="text", source="seed:standing_answers.json")
    page = tmp_path / "dell.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = snap = await snapshot.take(ex)
        ref = next(i["ref"] for i in snap.items if "Postal" in (i.get("label") or ""))
        assert (await ex.read_ref(ref)).get("value") == "77479"
        from goat import readback
        assert (await readback.refresh_values(ex, snap, [ref])).get(ref) == "77479"
        out, _ = await d.t_fill_batch([{"ref": ref, "answer_key": "identity.zip", "value": None, "source": "computer", "label": "Postal Code"}])
        assert out["results"][0]["status"] == "kept", out
    finally:
        await ex.close(close_browser=True)
