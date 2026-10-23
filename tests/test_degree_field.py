"""ABB (Workday, 2026-10-23): the Degree list offered 'B.S.M.E. - Bachelor of Science in Mechanical Engineering' first and
the degree rule took the first science bachelor's for his 'Bachelor of Science'; the judge failed the draft. His own field
comes first, then a plain science bachelor's; another field's is never his."""
from goat.browser import fill

CHAIN = ["Bachelor of Science", "Bachelor's Degree", "Bachelor's", "Bachelors", "Bachelor", "Undergraduate"]
BSME = "B.S.M.E. - Bachelor of Science in Mechanical Engineering"


def _pick(*texts):
    hit = fill.best_option([{"text": t} for t in texts], CHAIN)
    return hit["text"] if hit else None


def test_a_plain_science_bachelors_beats_another_fields():
    assert _pick(BSME, "B.S. - Bachelor of Science", "B.A. - Bachelor of Arts") == "B.S. - Bachelor of Science"


def test_his_own_field_comes_first():
    assert _pick(BSME, "B.S. - Bachelor of Science", "B.S.C.S. - Bachelor of Science in Computer Science") == \
        "B.S.C.S. - Bachelor of Science in Computer Science"
    assert _pick("Bachelor of Science in Computer Engineering", "Bachelor of Science in Nursing") == \
        "Bachelor of Science in Computer Engineering"


def test_only_other_fields_fall_through_to_the_rest_of_his_chain():
    assert _pick(BSME, "B.S.N. - Bachelor of Science in Nursing", "Bachelor's Degree") == "Bachelor's Degree"
    assert _pick(BSME, "B.S.N. - Bachelor of Science in Nursing") is None


def test_an_exact_answer_still_wins():
    assert fill.best_option([{"text": BSME}, {"text": "Bachelor of Science"}], ["Bachelor of Science"])["text"] == "Bachelor of Science"
