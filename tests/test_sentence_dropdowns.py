"""Workday 'Select One' menus whose options are sentences under labels longer than the read-back window (Booz Allen's
Application Questions): a stored No takes the single negative sentence, the pick is verified from the button's own text
(its value attribute is an internal id), and a list left open by a failed pick never blocks the next control."""
import pytest

from goat import db

PAGE = """<!doctype html><html><body style="margin:40px">
<div data-automation-id="formField-q5"><label for="q5">5. U.S. Federal Government Employment* Various federal laws restrict the post-government employment activities of former federal government employees. These laws also place conditions on current federal government employees who engage in discussions regarding post-government employment and may include an obligation to notify an ethics official of this application and to recuse from matters involving this company.</label>
<button id="q5" data-goat-ref="e21" aria-haspopup="listbox" type="button">Select One</button></div>
<div data-automation-id="formField-q6" style="margin-top:300px"><label for="q6">6. State Government Employment* Various state laws restrict the post-government employment activities of former state government employees. These laws also place conditions on current state government employees who engage in discussions regarding post-government employment and may include an obligation to notify an ethics official of this application and to recuse from matters involving this company.</label>
<button id="q6" data-goat-ref="e22" aria-haspopup="listbox" type="button">Select One</button></div>
<script>
  const OPTS = {
    q5: ['Select One', 'I have never been employed by the U.S. Federal Government.', 'I am currently employed by the U.S. Federal Government.',
         'I am a former employee of U.S. Federal Government that left service in the last 2 years.'],
    q6: ['Select One', 'I have never been employed by a Government Entity, or political party.', 'I am currently employed by a Government Entity.']
  };
  function open(btn) {
    if (document.getElementById('lb')) return;
    const ul = document.createElement('ul'); ul.id = 'lb'; ul.setAttribute('role', 'listbox');
    ul.style.cssText = 'position:absolute;left:40px;top:' + (btn.getBoundingClientRect().bottom + window.scrollY) + 'px;background:#fff;border:1px solid #888;margin:0;padding:0;list-style:none';
    for (const t of OPTS[btn.id]) {
      const li = document.createElement('li'); li.setAttribute('role', 'option'); li.textContent = t; li.style.cssText = 'display:block;height:24px;padding:2px 8px';
      li.addEventListener('click', (e) => { e.stopPropagation(); btn.textContent = t; btn.value = '2cdc26b8594510011a2b1dffae0e0002'; ul.remove(); });
      ul.appendChild(li);
    }
    document.body.appendChild(ul);
  }
  for (const id of ['q5', 'q6']) document.getElementById(id).addEventListener('click', (e) => { e.stopPropagation(); open(e.currentTarget); });
  // Like Workday: Escape does nothing; only a mousedown outside the menu closes it.
  document.addEventListener('mousedown', (e) => { const l = document.getElementById('lb'); if (l && !l.contains(e.target)) l.remove(); });
</script></body></html>"""


async def _open(tmp_path):
    from goat.browser.executor import Executor, HostPolicy
    db.reset_for_tests(tmp_path / "t.db")
    page = tmp_path / "sentences.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
    return ex


@pytest.mark.asyncio
async def test_no_takes_the_single_negative_sentence(tmp_path):
    from goat.browser import fill
    ex = await _open(tmp_path)
    try:
        r = await fill.choose(ex, "e21", ["No"])
        assert r.ok, r.error
        assert (await ex.page.inner_text("#q5")).startswith("I have never been employed")
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_list_left_open_by_a_failed_pick_does_not_block_the_next_control(tmp_path):
    from goat.browser import fill
    ex = await _open(tmp_path)
    try:
        r = await fill.choose(ex, "e21", ["Maybe"])          # nothing matches: the menu would stay open on Workday
        assert not r.ok
        assert await ex.page.evaluate("() => !document.getElementById('lb')"), "the failed pick left its list open"
        r2 = await fill.choose(ex, "e22", ["No"])
        assert r2.ok, r2.error
        assert (await ex.page.inner_text("#q6")).startswith("I have never been employed")
        assert await ex.page.inner_text("#q5") == "Select One"
    finally:
        await ex.close(close_browser=True)


def test_chip_matching_accepts_an_issuer_suffix_but_not_an_issuer_acronym():
    from goat.browser.fill import _chip_matches
    assert _chip_matches("AWS Certified Cloud Practitioner - Amazon Web Services (AWS)", ["AWS Certified Cloud Practitioner"])
    assert _chip_matches("Structured Query Language (SQL)", ["SQL"])
    assert _chip_matches("Python (Programming Language)", ["Python"])
    assert not _chip_matches("AWS Business Professional - Amazon Web Services (AWS)", ["AWS"])
    assert not _chip_matches("AWS Business Professional - Amazon Web Services (AWS)", ["AWS Certified Cloud Practitioner"])
    assert not _chip_matches("Python IDLE", ["Python"])


def test_a_category_prefixed_source_counts_as_that_source():
    """KLA (Workday, 2026-10-23): searching 'LinkedIn' found 'Job Board: LinkedIn', which was refused as no match."""
    from goat.browser.fill import _chip_matches
    assert _chip_matches("Job Board: LinkedIn", ["LinkedIn"])
    assert _chip_matches("Job Board - LinkedIn", ["LinkedIn"])
    assert not _chip_matches("Job Board: LinkedIn", ["Indeed"])
    assert not _chip_matches("Previously employed: No", ["No"])


def test_a_category_with_another_named_source_is_not_his_fallback():
    """KLA (Workday, 2026-10-23): with 'Job Board: LinkedIn' out of view, 'Job Board: Facebook' was taken for the fallback
    'Job Board' and the application went out saying he heard of KLA on Facebook."""
    from goat.browser.fill import _chip_matches, best_option
    wants = ["LinkedIn", "Job Board", "Agency / Search Firm", "Other"]
    shown = [{"text": t} for t in ["Agency / Search Firm", "Job Board: Facebook", "Job Board: Indeed", "Other"]]
    assert best_option(shown, wants)["text"] == "Agency / Search Firm"
    assert best_option([{"text": "Job Board: Facebook"}], wants) is None
    assert not _chip_matches("Job Board: Facebook", ["Job Board"])
    full = shown + [{"text": "Job Board: LinkedIn"}]
    assert best_option(full, wants)["text"] == "Job Board: LinkedIn"
    # a category with a generic leaf is still the category
    assert best_option([{"text": "Job Board: Other"}], wants)["text"] == "Job Board: Other"
    # an issuer after a dash is still the credential
    assert _chip_matches("AWS Certified Cloud Practitioner - Amazon Web Services (AWS)", ["AWS Certified Cloud Practitioner"])
