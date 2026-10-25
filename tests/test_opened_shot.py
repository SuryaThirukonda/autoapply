"""LabCorp (Workday, 2026-10-25): both screenshot captures timed out when the page opened and the run ended as
'error: Page.screenshot' before anything was filled. The opening screenshot is a record, never a step that can end a run."""
import inspect
import re

from goat import worker


def test_the_opening_screenshot_is_best_effort():
    src = inspect.getsource(worker)
    m = re.search(r"try:\s*\n\s*png = await ex\.screenshot_png\(\)\s*\n\s*records\.save_screenshot\(job_id, png, \"opened\"\)\s*\n"
                  r"\s*except Exception", src)
    assert m, "the 'opened' screenshot must sit in its own try/except"
