"""Allstate's Workday list spelled its option 'Sophmore' (2026-10-23): no stored wording matched it, the chain fell through to
his credit-hours fallback 'Junior', and the judge held the draft. One letter off a long word is the same answer."""
from goat.browser import fill

CHAIN = ["Sophomore", "2nd year", "Second year", "Sophomore (2nd year)", "Junior", "3rd year", "Third year"]


def _opts(*texts):
    return [{"text": t} for t in texts]


def test_a_portal_misspelling_beats_a_later_fallback_in_the_chain():
    hit = fill.best_option(_opts("Select One", "Freshman", "Sophmore", "Junior", "Senior", "Graduate"), CHAIN)
    assert hit["text"] == "Sophmore"


def test_one_edit():
    assert fill._one_edit("sophomore", "sophmore") and fill._one_edit("sophmore", "sophomore")
    assert fill._one_edit("receive", "recieve")                      # two letters swapped
    assert fill._one_edit("bachelor", "bachelar")
    assert not fill._one_edit("junior", "senior") and not fill._one_edit("same", "same")
    assert not fill._one_edit("sophomore", "sophmor")                # two letters dropped


def test_short_words_numbers_and_schools_are_never_guessed():
    assert fill.best_option(_opts("Yea", "Nay"), ["Yes"]) is None
    assert fill.best_option(_opts("Class of 2029", "Class of 2030"), ["Class of 2028"]) is None
    assert fill.best_option(_opts("Texas A&I University"), ["Texas A&M University"]) is None


def test_two_options_one_letter_off_are_ambiguous():
    hit = fill.best_option(_opts("Sophmore", "Sophomores", "Junior"), CHAIN)
    assert hit["text"] == "Junior"                                   # no guess between the two: the chain goes on


def test_a_correct_spelling_still_wins_outright():
    assert fill.best_option(_opts("Freshman", "Sophomore", "Junior"), CHAIN)["text"] == "Sophomore"


SCHOOL = ["Texas A&M University", "Texas A & M University", "Texas A&M University - College Station",
          "Texas A&M University-College Station", "TX-Texas A&M University", "Texas A&M University College Station", "Other"]


def test_the_lists_shorter_name_for_his_school_beats_other():
    """L3Harris (2026-10-23): the list offered 'Texas A&M'; the chain fell through to 'Other' and it went in that way."""
    hit = fill.best_option(_opts("Other", "Texas A&M", "Texas Tech University", "University of Texas at Austin"), SCHOOL)
    assert hit["text"] == "Texas A&M"


def test_a_one_word_or_other_school_prefix_is_never_his_school():
    assert fill.best_option(_opts("Texas", "Other"), SCHOOL)["text"] == "Other"
    assert fill.best_option(_opts("Texas A&M University - Commerce", "Other"), SCHOOL)["text"] == "Other"
