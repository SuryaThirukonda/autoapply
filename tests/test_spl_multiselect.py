"""AbbVie (SmartRecruiters, 2026-10-24): 'What is your current degree major?' and 'Which location(s) are you not willing to
work in?' are spl-multiselect-autocomplete fields. The pick lives on the host (an array of option ids) and as a chip
(<spl-tag label=...>) in the tags list; the inner search <input> is empty. The gate read both as empty required fields and
the run held twice."""
from goat import db
from goat.browser import errors, snapshot
from goat.browser.executor import Executor, HostPolicy

PAGE = """<html><body style="font:14px sans-serif">
<label id="lab">What is your current degree major or area of focus? *</label>
<spl-multiselect-autocomplete id="ms" aria-labelledby="lab"></spl-multiselect-autocomplete>
<script>
customElements.define('spl-tag', class extends HTMLElement { connectedCallback() {
  if (!this.shadowRoot) this.attachShadow({ mode: 'open' }).innerHTML = '<span>' + this.getAttribute('label') + ' x</span>'; } });
customElements.define('spl-tags-list', class extends HTMLElement { connectedCallback() {
  if (!this.shadowRoot) this.attachShadow({ mode: 'open' }).innerHTML = '<div><spl-tag label="Computer Science"></spl-tag><slot></slot></div>'; } });
customElements.define('spl-multiselect-autocomplete', class extends HTMLElement { connectedCallback() {
  if (this.shadowRoot) return;
  this.value = ['becac50b-07e3-4301-b311-101bb608d7b2'];
  this.attachShadow({ mode: 'open' }).innerHTML =
    '<div><spl-tags-list><div><input role="combobox" aria-required="true" aria-label="degree major *" style="width:200px;height:24px"></div></spl-tags-list></div>'; } });
</script></body></html>"""


async def test_a_picked_spl_multiselect_is_not_an_empty_required_field(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(PAGE)
        await ex.page.wait_for_timeout(200)
        scan = await errors.scan_errors(ex)
        assert not scan.get("empty_required"), scan.get("empty_required")
        snap = await snapshot.take(ex)
        combo = next(i for i in snap.items if i.get("role") == "combobox" or "major" in (i.get("label") or "").lower())
        read = await ex.read_ref(combo["ref"])
        assert "Computer Science" in str(read.get("shown") or read.get("value") or ""), read
    finally:
        await ex.close()
