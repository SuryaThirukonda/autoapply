"""Western Digital (SmartRecruiters, 2026-10-25): 'Which programming languages have you used?' offered C, C++, C#, Python ...;
his chain 'C++; Python; Java; JavaScript' picked 'C' because the comparison dropped the pluses."""
from goat.browser import fill

LANGS = [{"text": t} for t in ("C", "C#", "C++", "Go", "Java", "JavaScript", "Python")]


def test_cplusplus_is_not_c():
    assert fill.best_option(LANGS, ["C++"])["text"] == "C++"
    assert fill.best_option(LANGS, ["C#"])["text"] == "C#"
    assert fill.best_option([{"text": "C"}, {"text": "Rust"}], ["C++"]) is None
    assert fill.best_option(LANGS, ["Python"])["text"] == "Python"
    assert fill.best_option(LANGS, ["Java"])["text"] == "Java"
