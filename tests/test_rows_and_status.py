"""Westinghouse and Smith+Nephew (2026-10-23).

- The park check filled the new employment row the run had added for his Rathinam Group entry with Southwest Airlines and
  his internship start date: a label the page repeats belongs to one row of a repeated section and is never filled by a
  lookup of its words.
- 'Date Obtained 1' in the citizenship block was filled from his date of birth with nothing saying why; the judge refused
  it. The date now comes from compliance.citizenship_obtained_date, whose note says he is a citizen by birth.
- Smith+Nephew's postings that take expressions of interest until they formally open were held as eligibility."""
import pytest

from goat import answers, db
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

ROWS = """<!doctype html><html><body style="margin:20px">
<form onsubmit="event.preventDefault();">
  <h3>Previous Employment</h3>
  <label for="c1">* Company Name</label><input id="c1" type="text" required value="Revio"><br>
  <label for="t1">* Title</label><input id="t1" type="text" required value="Founding Software Engineer"><br>
  <label for="c2">* Company Name</label><input id="c2" type="text" required><br>
  <label for="t2">* Title</label><input id="t2" type="text" required><br>
  <label for="sig">* Signature</label><input id="sig" type="text" required><br>
  <button type="submit">Apply</button>
</form></body></html>"""

CITIZEN = """<!doctype html><html><body style="margin:20px">
<form onsubmit="event.preventDefault();">
  <label for="cn">* Country 1 Name</label><input id="cn" type="text" required value="United States"><br>
  <label for="ts">* Type of Status 1</label><input id="ts" type="text" required value="Citizen"><br>
  <label for="do">* Date Obtained 1</label><input id="do" type="text" required placeholder="MM/DD/YYYY"><br>
  <button type="submit">Apply</button>
</form></body></html>"""


async def _open(tmp_path, html, name):
    page = tmp_path / name
    page.write_text(html, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
    return ex


@pytest.mark.asyncio
async def test_a_park_never_fills_a_row_of_a_repeated_section(store, tmp_path):
    answers.save_answer("experience.work.5.company", "Southwest Airlines", answer_type="text", source="questionnaire",
                        aliases=["Company Name"])
    answers.save_answer("bank.signature", "Surya Thirukonda", answer_type="text", source="seed:answer_bank.json",
                        aliases=["Signature"])
    ex = await _open(tmp_path, ROWS, "rows.html")
    try:
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = await snapshot.take(ex)
        await d.t_park("needs_you: the second employment row and the signature are empty")
        vals = await ex.page.evaluate("() => ['c2', 't2', 'sig'].map(i => document.getElementById(i).value)")
        assert vals == ["", "", "Surya Thirukonda"], vals
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_the_citizenship_date_carries_its_reason(store, tmp_path):
    answers.save_answer("compliance.date_of_birth", "01/02/2003", answer_type="text", source="questionnaire")
    ex = await _open(tmp_path, CITIZEN, "citizen.html")
    try:
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = snap = await snapshot.take(ex)
        ref = next(i["ref"] for i in snap.items if "Date Obtained" in (i.get("label") or ""))
        out, _ = await d.t_lookup_answer("Date Obtained 1", [], None, ref)
        assert out["key"] == "compliance.citizenship_obtained_date" and out["answer"] == "01/02/2003", out
        res, _ = await d.t_fill_batch([{"ref": ref, "answer_key": "compliance.date_of_birth", "value": None, "source": None,
                                        "label": "Date Obtained 1"}])
        assert res["results"][0]["status"] == "set", res
        src = db.connect().execute("SELECT source FROM decisions WHERE ref=? ORDER BY id DESC LIMIT 1", (ref,)).fetchone()[0]
        assert src == "standing:compliance.citizenship_obtained_date"
        assert "citizen by birth" in answers.get("compliance.citizenship_obtained_date").note
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_an_expression_of_interest_posting_is_applied_to(store):
    d, ctx = _dispatcher(_job())

    async def page_text(n=0):
        return "Intern Embedded Software Engineering Apply"
    ctx.ex.page_text = page_text
    out, _ = await d.t_park("eligibility: The posting states that the position is not currently available for hiring and is only "
                            "accepting expressions of interest until it formally opens.")
    assert "expression of interest" in out["error"], out


@pytest.mark.asyncio
async def test_an_eligibility_hold_quoting_years_the_posting_never_states_is_refused(store):
    """Cisco 'Embedded Systems I' (2026-10-23) was held for '2 years of work experience', a rule of its '... II' siblings."""
    from goat.agent.tools import posting_states_years
    assert posting_states_years(2, "An undergraduate degree program with 2 years of work experience")
    assert posting_states_years(2, "2+ years of experience with embedded systems")
    assert posting_states_years(2, "two years of industry experience") and posting_states_years(3, "3-5 years")
    assert not posting_states_years(2, "Currently enrolled in an undergraduate degree program. Proficient in C.")
    d, ctx = _dispatcher(_job())
    ctx.posting_text = "Minimum Qualifications: Currently enrolled in an undergraduate degree program. Proficient in C."

    async def page_text(n=0):
        return "Software Engineer Embedded Systems I (Intern) Apply"
    ctx.ex.page_text = page_text
    out, _ = await d.t_park("eligibility: Cisco requires undergraduate applicants to have 2 years of work experience; Surya's "
                            "documented professional experience does not meet that minimum.")
    assert "does not state" in out["error"], out


def test_a_plain_no_never_picks_an_option_that_says_he_graduated():
    """Nintendo (2026-10-23): his co-op answer No picked 'Not Applicable, Graduated' for the Fall 2027 term."""
    from goat.browser import fill
    opts = [{"text": "August 2027"}, {"text": "Not Applicable, Graduated"}]
    assert fill.best_option(opts, ["No"]) is None
    assert fill.best_option([{"text": "Yes"}, {"text": "Not interested"}], ["No"])["text"] == "Not interested"


@pytest.mark.asyncio
async def test_he_requires_no_housing_accommodation(store):
    """Nintendo (2026-10-23): 'Require housing accommodation?' went out Yes from his 'can arrange his own housing' Yes."""
    answers.save_answer("availability.own_housing", "Yes", answer_type="choice", source="questionnaire")
    d, ctx = _dispatcher(_job())
    out, _ = await d.t_lookup_answer("Will you require housing accommodation during the internship?", ["Yes", "No"], None, None)
    assert out["answer"] == "No" and out["via"] == "housing_rule", out
    out, _ = await d.t_lookup_answer("Would you like a housing stipend?", ["Yes", "No"], None, None)
    assert out.get("via") != "housing_rule"


@pytest.mark.asyncio
async def test_an_eligibility_park_about_a_form_field_is_refused(store):
    """Waymo (2026-10-23): 'eligibility: required education start/end date fields only support month/year ...'."""
    d, ctx = _dispatcher(_job())

    async def page_text(n=0):
        return "Education Start date End date Submit your application"
    ctx.ex.page_text = page_text
    out, _ = await d.t_park("eligibility: required education start/end date fields only support month/year and the form's "
                            "education record dates need clarification; current student enrollment dates unknown")
    assert "names a form field" in out["error"], out


@pytest.mark.asyncio
async def test_technology_experience_is_no_eligibility_rule(store):
    """'<technology> experience not established' names skills, not an eligibility rule (Neuralink, 2026-10-23); FPGA or other
    hardware work stays a disqualifier."""
    d, ctx = _dispatcher(_job())

    async def page_text(n=0):
        return "Firmware Engineer Intern Apply"
    ctx.ex.page_text = page_text
    out, _ = await d.t_park("eligibility: the role requires React, GraphQL and Kubernetes experience not established in "
                            "Surya's profile")
    assert "not an eligibility rule" in out["error"], out


STANDING = """<!doctype html><html><body>
<div data-automation-id="formField-standing"><label for="sd">Academic standing at assignment start*</label>
<button id="sd" data-goat-ref="e970" aria-haspopup="listbox" type="button">Senior</button></div>
<script>
  const btn = document.getElementById('sd');
  btn.addEventListener('click', () => {
    if (document.getElementById('lb')) return;
    const ul = document.createElement('ul'); ul.id = 'lb'; ul.setAttribute('role', 'listbox');
    ul.style.cssText = 'position:absolute;top:40px;left:10px;width:220px;margin:0;padding:0;background:#fff;border:1px solid #888';
    for (const t of ['Freshman', 'Sophomore', 'Junior', 'Senior']) {
      const li = document.createElement('li'); li.setAttribute('role', 'option'); li.textContent = t;
      li.style.cssText = 'display:block;height:24px;list-style:none';
      li.addEventListener('click', () => { btn.textContent = t; ul.remove(); });
      ul.appendChild(li);
    }
    document.body.appendChild(ul);
  });
</script></body></html>"""


@pytest.mark.asyncio
async def test_a_held_fallback_gives_way_to_his_first_preference(tmp_path):
    """Carrier (2026-10-23): 'Senior' (a fallback of 'Junior; ...; Senior') was kept and never changed to Junior."""
    from goat import db as _db
    from goat.browser import fill
    _db.reset_for_tests(tmp_path / "t.db")
    ex = await _open(tmp_path, STANDING, "standing.html")
    try:
        r = await fill.choose(ex, "e970", ["Junior", "3rd year", "Senior"])
        assert r.ok, r.error
        assert await ex.page.inner_text("#sd") == "Junior"
    finally:
        await ex.close(close_browser=True)
