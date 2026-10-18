"""Save a scrubbed copy of a job's current apply page as an offline test fixture.

  python scripts/save_fixture.py <job_id> [--name lyft-employment]

Attaches to the job's own tab in the dedicated Chrome (jobs.tab_target_id), takes the page HTML, strips
personal values (emails, phones, typed input values, textarea bodies, the résumé file name, goat's own
data-goat-ref marks) and external scripts, and writes tests/fixtures/<portal>/<name>.html next to a
<name>.expect.json holding the labels and headings a snapshot of the saved file produces, so
tests/test_fixture_replay.py can assert the outline still reads them. Nothing on the page is changed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<![\w/.-])\+?\d[\d\s().-]{8,}\d(?![\w/.-])")
VALUE_ATTR = re.compile(r"(<(?:input|option)\b[^>]*?\svalue=)(\"[^\"]*\"|'[^']*')", re.I)
TEXTAREA = re.compile(r"(<textarea\b[^>]*>)(.*?)(</textarea>)", re.I | re.S)
SCRIPT_SRC = re.compile(r"<script\b[^>]*\bsrc=[^>]*>\s*</script>", re.I | re.S)
GOAT_REF = re.compile(r"\sdata-goat-ref=\"[^\"]*\"")
RESUME = re.compile(r"[\w-]*(resume|résumé|cv)[\w-]*\.(pdf|docx?)", re.I)


def scrub(html: str, keep_option_values: bool = True) -> str:
    """Personal data out, structure intact. Option values stay (tests pick them), input values go."""
    out = SCRIPT_SRC.sub("", html)
    out = GOAT_REF.sub("", out)
    out = TEXTAREA.sub(lambda m: m.group(1) + m.group(3), out)

    def _val(m: re.Match) -> str:
        tag = m.group(0)[:7].lower()
        if keep_option_values and tag.startswith("<option"):
            return m.group(0)
        return m.group(1) + '""'

    out = VALUE_ATTR.sub(_val, out)
    out = EMAIL.sub("applicant@example.com", out)
    out = RESUME.sub("resume.pdf", out)
    out = PHONE.sub("000-000-0000", out)
    return out


async def _capture(job_id: int) -> tuple[str, str, str]:
    from goat import db
    from goat.browser.executor import Executor, HostPolicy
    con = db.connect()
    row = con.execute("SELECT j.tab_target_id, j.apply_url, l.ats, l.url FROM jobs j JOIN listings l ON l.id=j.listing_id WHERE j.id=?", (job_id,)).fetchone()
    if not row:
        raise SystemExit(f"no job {job_id}")
    if not row["tab_target_id"]:
        raise SystemExit(f"job {job_id} has no tab to attach to")
    ex = Executor(policy=HostPolicy([row["url"], row["apply_url"] or row["url"]]), job_id=job_id)
    await ex.start()
    try:
        page = await ex.attach(row["tab_target_id"])
        if page is None:
            raise SystemExit("the job's tab is gone")
        html = await ex.page.content()
        return html, row["ats"] or "other", ex.url
    finally:
        await ex.close()


async def _expectations(path: Path) -> dict:
    from goat.browser import snapshot
    from goat.browser.executor import Executor, HostPolicy
    url = path.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page()
        ex.policy.hosts.add("")
        await ex.page.goto(url)
        await ex.settle(quiet_ms=100)
        snap = await snapshot.take(ex)
        labels = [i.get("label") for i in snap.items if i.get("label")][:30]
        groups = [g.get("label") for g in snap.groups if g.get("label")][:10]
        return {"labels": labels, "groups": groups, "headings": list(snap.headings or [])[:8], "items": snap.total_items}
    finally:
        await ex.close(close_browser=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("job", type=int)
    ap.add_argument("--name", help="file stem (default: job-<id>)")
    a = ap.parse_args(argv)
    html, portal, url = asyncio.run(_capture(a.job))
    name = a.name or f"job-{a.job}"
    dest = ROOT / "tests" / "fixtures" / portal
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{name}.html"
    path.write_text(f"<!-- fixture from job {a.job} ({portal}); page scrubbed by scripts/save_fixture.py -->\n" + scrub(html), encoding="utf-8")
    exp = asyncio.run(_expectations(path))
    exp["source_host"] = re.sub(r"^https?://([^/]+).*$", r"\1", url)
    (dest / f"{name}.expect.json").write_text(json.dumps(exp, indent=1), encoding="utf-8")
    print(f"saved {path} ({len(html)} chars -> {path.stat().st_size} bytes), {exp['items']} items, {len(exp['labels'])} labels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
