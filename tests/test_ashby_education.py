"""Ashby's Education History (ibotta, 2026-10-19): "Invalid value" after the agent left an end date (May 2026) under a
ticked "Still Student?". The harness fills the block: school, major, "Still Student?" ticked, end date empty."""
import pytest

from goat import answers, db
from goat.adapters.ashby_education import fill_education
from goat.browser import snapshot
from tests.test_answer_rules import _job, store  # noqa: F401 - fixtures
from tests.test_page_readback import _race

MONTHS = "".join(f'<option value="{i}">{m}</option>' for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1))
YEARS = "".join(f"<option>{y}</option>" for y in range(2027, 1989, -1))


def page(month="", year="", current=False):
    sel = lambda v: f' data-start="{v}"'
    return f"""<!doctype html><html><body style="margin:20px">
<div data-field-path="_systemfield_education_history"><label for="_systemfield_education_history">Education History</label>
 <div><span>Education</span><button disabled>Delete</button>
  <div><label for="_systemfield_education_history-school">School</label>
   <input placeholder="Search schools..." aria-autocomplete="list" role="combobox" id="sch"><ul id="res" role="listbox"></ul></div>
  <div><label for="_systemfield_education_history-major">Field of Study</label>
   <input id="_systemfield_education_history-major" placeholder="e.g. Computer Science" type="text"></div>
  <div><label for="_systemfield_education_history-endDate">End Date</label>
   <div id="_systemfield_education_history-endDate">
    <select id="mon"{sel(month)}><option value="">Month...</option>{MONTHS}</select>
    <select id="yr"{sel(year)}><option value="">Year...</option>{YEARS}</select></div></div>
  <label for="_systemfield_education_history-isCurrent"><input type="checkbox" id="_systemfield_education_history-isCurrent"> Still Student?</label>
 </div></div>
<script>
  const SCHOOLS = ["Texas A&M University", "Texas A&M University - Commerce", "Texas State University"];
  const inp = document.getElementById('sch'), res = document.getElementById('res');
  inp.addEventListener('input', () => {{
    const q = inp.value.trim().toLowerCase(); res.innerHTML = '';
    if (!q) return;
    for (const u of SCHOOLS.filter(u => u.toLowerCase().includes(q))) {{
      const li = document.createElement('li'); li.setAttribute('role', 'option'); li.textContent = u;
      li.addEventListener('mousedown', (e) => {{ e.preventDefault(); inp.value = u; res.innerHTML = ''; }});
      res.appendChild(li);
    }}
  }});
  const mon = document.getElementById('mon'), yr = document.getElementById('yr');
  const cur = document.getElementById('_systemfield_education_history-isCurrent');
  mon.value = mon.dataset.start; yr.value = yr.dataset.start;
  // like Ashby: ticking the box disables the end date but keeps whatever it held
  const sync = () => {{ mon.disabled = cur.checked; yr.disabled = cur.checked; }};
  cur.checked = {str(current).lower()}; sync();
  cur.addEventListener('change', sync);
</script></body></html>"""


def _seed():
    answers.save_answer("education.school", "Texas A&M University", source="questionnaire")
    answers.save_answer("education.school_variants", "Texas A & M University; Texas A&M", answer_type="choice_chain", source="operator")
    answers.save_answer("education.major", "Computer Science", source="questionnaire")
    answers.save_answer("education.graduation_month_year", "May 2028", source="questionnaire")


async def _state(ex):
    return await ex.page.evaluate("""() => ({school: document.getElementById('sch').value,
        major: document.getElementById('_systemfield_education_history-major').value,
        month: document.getElementById('mon').value, year: document.getElementById('yr').value,
        current: document.getElementById('_systemfield_education_history-isCurrent').checked})""")


@pytest.mark.asyncio
@pytest.mark.parametrize("start", [dict(), dict(month="5", year="2026", current=True), dict(month="5", year="2026", current=False)])
async def test_education_history_is_his_school_major_and_still_a_student(store, tmp_path, start):
    _seed()
    job = _job()
    ex = await _race(tmp_path, page(**start), "edu.html")
    try:
        await snapshot.take(ex)
        rep = await fill_education(ex, job, 1)
        assert rep and not rep["failed"], rep
        assert await _state(ex) == {"school": "Texas A&M University", "major": "Computer Science", "month": "", "year": "", "current": True}
        con = db.connect()
        labels = {r[0] for r in con.execute("SELECT field_label FROM decisions WHERE job_id=?", (job["id"],))}
        assert {"Education History / School", "Education History / Field of Study", "Education History / Still Student?"} <= labels
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_page_without_the_field_is_left_alone(store, tmp_path):
    ex = await _race(tmp_path, "<!doctype html><html><body><label for=a>School</label><input id=a></body></html>", "none.html")
    try:
        await snapshot.take(ex)
        assert await fill_education(ex, _job(), 1) is None
    finally:
        await ex.close(close_browser=True)
