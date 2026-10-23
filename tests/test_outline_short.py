"""Westinghouse (SuccessFactors, 2026-10-23): the whole application is one page, and its outline ran past 14,000
characters. The cut dropped the start-date box, every work-entry date and the Apply button, so the model never filled the
start date and the judge failed the draft for it. A page too long for the full outline is now written short first."""
from goat.browser import snapshot
from goat.browser.snapshot import Snapshot


def _snap(items):
    return Snapshot(url="https://career4.successfactors.com/portalcareer?company=WestinghouseP", title="Apply", headings=[],
                    items=items, groups=[], errors={}, challenge={}, total_items=len(items))


def _westinghouse_like():
    items, n = [], 0

    def add(**kw):
        nonlocal n
        n += 1
        kw.setdefault("ref", f"e{n}")
        kw.setdefault("bbox", [310, -6000 + n * 40])
        kw.setdefault("inview", False)
        items.append(kw)
    for k in range(40):
        add(tag="input", type="text", label=f"* Picklist question number {k} about something long enough", value="No", required=True)
        add(tag="button", label=f"Picklist question number {k} about something long enough")
    for q in ("* How many years of experience do you have related to the position for which you are applying?",
              "* Will you now or in the future require sponsorship for employment visa status (e.g., H-1B visa status)?"):
        for opt in ("None", "1 to 2 Years", "3 to 5 Years"):
            add(tag="toggle", label=opt, group=q, pressed=opt == "1 to 2 Years")
    add(tag="button", label="Apply", submitish=True, inview=True, bbox=[1222, 770])
    for _ in range(2):
        add(tag="input", type="text", label="Start Date", value="02/01/2026", required=True)
    add(tag="input", type="text", label="When are you available to start work?", value="", placeholder="MM/DD/YYYY")
    return items


def test_a_long_page_is_written_short_and_keeps_every_ref():
    items = _westinghouse_like()
    s = _snap(items)
    full = snapshot.render(s, max_chars=10**9)
    out = snapshot.render(s, max_chars=9000)
    assert len(full) > 9000 and len(out) <= 9000, (len(full), len(out))
    assert "outline truncated" not in out
    for i in items:
        assert f"[{i['ref']}]" in out, i
    assert 'When are you available to start work?" value=""' in out


def test_the_short_form_folds_pickers_groups_toggles_and_keeps_positions_where_labels_repeat():
    items = _westinghouse_like()
    out = snapshot.render(_snap(items), max_chars=9000)
    assert '[e1] text "* Picklist question number 0 about something long enough" required value="No" (picker button [e2])' in out
    assert "\n[e2] button" not in out
    assert out.count("toggles for question") == 2
    assert '[e81] "None" [ ] [e82] "1 to 2 Years" [x] [e83] "3 to 5 Years" [ ]' in out
    starts = [l for l in out.splitlines() if '"Start Date"' in l]
    assert len(starts) == 2 and all("@(" in l for l in starts)          # repeated labels keep their place
    assert '"Apply" SUBMIT-CONTROL(only submit() may click)' in out


def test_a_page_that_fits_keeps_the_full_form():
    items = _westinghouse_like()[:6]
    out = snapshot.render(_snap(items))
    assert "picker button" not in out and "@(" in out and "offscreen" in out
