"""scripts/save_fixture.py: the saved page keeps its structure and loses the applicant's data."""
import importlib.util
from pathlib import Path


def _mod():
    p = Path(__file__).parent.parent / "scripts" / "save_fixture.py"
    spec = importlib.util.spec_from_file_location("save_fixture", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_scrub_removes_values_emails_phones_and_goat_marks():
    scrub = _mod().scrub
    html = ('<script src="https://cdn.example/app.js"></script><script>window.x = 1;</script>'
            '<label for="e">Email</label><input id="e" data-goat-ref="e4" value="thirukondas@gmail.com">'
            '<input id="p" type="tel" value="281-832-1205"><p>Call 281-832-1205 or write thirukondas@gmail.com</p>'
            '<select><option value="tamu" selected>Texas A&amp;M</option></select>'
            '<textarea id="why">my secret essay</textarea><span>Surya_Thirukonda_Resume_SWE.pdf</span>')
    out = scrub(html)
    assert "cdn.example" not in out and "window.x = 1" in out
    assert "data-goat-ref" not in out
    assert 'value=""' in out and "thirukondas" not in out and "281-832-1205" not in out
    assert "applicant@example.com" in out and "000-000-0000" in out
    assert 'value="tamu"' in out                      # option values survive
    assert "<textarea id=\"why\"></textarea>" in out
    assert "Surya_Thirukonda_Resume_SWE.pdf" not in out and "resume.pdf" in out
    assert '<label for="e">Email</label>' in out
