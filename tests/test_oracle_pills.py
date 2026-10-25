"""UL Solutions (Oracle, 2026-10-24): Yes/No and Mr./Ms. are <button role=radio aria-checked> pills. A computer-tool pick of
'No' read back as kept because the button's text says 'No', while 'Yes' stayed selected; and the prefill sourced the 'Yes' left
pressed by an earlier attempt from his general prior-internship answer."""
import pytest

from goat import answers, db
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

PILLS = """<!doctype html><html><body style="margin:20px"><form>
<div role="radiogroup" aria-label="Have you previously completed an internship at UL?">
  <span>Have you previously completed an internship at UL?</span>
  <ul><li role="presentation"><button type="button" role="radio" aria-checked="true" class="cx-select-pill-section">Yes</button></li>
      <li role="presentation"><button type="button" role="radio" aria-checked="false" class="cx-select-pill-section">No</button></li></ul>
</div></form></body></html>"""


async def _page(tmp_path):
    page = tmp_path / "ul.html"
    page.write_text(PILLS, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
    return ex


@pytest.mark.asyncio
async def test_an_unselected_pill_is_never_kept_by_its_text(store, tmp_path):
    answers.save_answer("background.previously_employed_at_this_company", "No", answer_type="choice", source="questionnaire")
    ex = await _page(tmp_path)
    try:
        d, ctx = _dispatcher(_job("UL Solutions", "L9"))
        ctx.ex = ex
        ctx.last_snapshot = snap = await snapshot.take(ex)
        no_ref = next(i["ref"] for i in snap.items if i.get("tag") == "toggle" and (i.get("label") or "").strip() == "No")
        out, _ = await d.t_fill_batch([{"ref": no_ref, "answer_key": "background.previously_employed_at_this_company",
                                        "value": None, "source": "computer", "label": "Internship at UL / No"}])
        r = out["results"][0]
        assert r["status"] == "failed" and "not selected" in r["error"], r
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_the_prefill_never_sources_an_internship_here_from_his_general_answer(store, tmp_path):
    from goat.adapters import for_portal
    answers.save_answer("bank.prior-internship", "Yes", answer_type="choice", source="seed:answer_bank.json")
    answers.add_alias('[["previously completed", "internship"], ["completed at least 1 internship"]]', "bank.prior-internship",
                      kind="substrings")
    ex = await _page(tmp_path)
    try:
        job = _job("UL Solutions", "L9")
        snap = await snapshot.take(ex)
        adapter = for_portal("oracle")
        await adapter.prefill(ex, snap, job, 1)
        rows = db.connect().execute("SELECT field_label, source FROM decisions WHERE job_id=?", (job["id"],)).fetchall()
        assert not any("internship at UL" in r[0] for r in rows), rows
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_his_general_prior_internship_yes_never_answers_an_internship_here(store, tmp_path):
    """UL Solutions (2026-10-24) went out with 'Have you previously completed an internship at UL? Yes': the model named
    bank.prior-internship (Yes, DeKalb) directly after the final check misread it as his answer to that question."""
    from goat import judge
    answers.save_answer("bank.prior-internship", "Yes", answer_type="choice", source="seed:answer_bank.json")
    ex = await _page(tmp_path)
    try:
        d, ctx = _dispatcher(_job("UL Solutions", "L9"))
        ctx.ex = ex
        ctx.last_snapshot = snap = await snapshot.take(ex)
        yes_ref = next(i["ref"] for i in snap.items if i.get("tag") == "toggle" and (i.get("label") or "").strip() == "Yes")
        out, _ = await d.t_fill_batch([{"ref": yes_ref, "answer_key": "bank.prior-internship", "value": None, "source": "answer_key",
                                        "label": "Have you previously completed an internship at UL?"}])
        r = out["results"][0]
        assert r["status"] == "failed" and "internship at UL Solutions itself" in r["error"], r
        out, _ = await d.t_choose(yes_ref, "bank.prior-internship", [], "Have you previously completed an internship at UL?", "answer_key")
        assert "internship at UL Solutions itself" in out.get("error", ""), out
        # a general internship question still takes it
        assert d._internship_here_refusal("bank.prior-internship", "Have you previously completed an internship?") is None
        assert "bank.prior-internship" in judge.JUDGE_INSTRUCTIONS
    finally:
        await ex.close(close_browser=True)


def test_a_public_institution_question_never_takes_his_federal_government_no(store):
    """HP (2026-10-24): 'government/public institution employment' took background.government_employee_current_or_former (No,
    federal only) by answer_key while the same question for HPE waited on Surya."""
    answers.save_answer("background.government_employee_current_or_former", "No", answer_type="choice", source="questionnaire")
    d, _ = _dispatcher(_job("HP", "L13"))
    err = d._public_institution_refusal("background.government_employee_current_or_former",
                                        "Have you been employed by a government or public institution in the past 5 years?")
    assert err and "public / state institutions" in err
    assert d._public_institution_refusal("background.government_employee_current_or_former",
                                         "Are you a current or former federal government employee?") is None
