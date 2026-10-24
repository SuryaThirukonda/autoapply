"""Watts Water (Workday, 2026-10-23): English Reading / Speaking / Writing went to 'Elementary' and choose called it 'this
portal's wording for Native': the highest level among ONE option is that option, so any level read back as his."""
from goat.browser.fill import best_option, proficiency_option

WANTS = ["Native", "Native or bilingual", "Fluent", "Native Speaker"]


def test_a_single_level_read_back_is_not_his_native_english():
    assert best_option([{"text": "Elementary"}], WANTS) is None
    assert proficiency_option([{"text": "Elementary"}], WANTS) is None


def test_a_scale_still_takes_its_highest_level():
    scale = [{"text": t} for t in ["Elementary", "Limited Working", "Professional Working", "Full Professional"]]
    assert best_option(scale, WANTS)["text"] == "Full Professional"
    assert best_option([{"text": "Native or Bilingual"}], WANTS)["text"] == "Native or Bilingual"
