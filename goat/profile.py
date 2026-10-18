"""Surya's drafting profile (config/profile.md): everything known about him that the agent may use when
it drafts a free-response answer or settles a yes/no question about his history. Read-only here; the
file is edited by hand (or by Claude) and versioned in the folder."""
from __future__ import annotations

import re

from .config import SETTINGS


def path():
    return SETTINGS.config_dir / "profile.md"


def text(max_chars: int = 40000) -> str:
    p = path()
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")[:max_chars]


def section(title: str, body: str | None = None) -> str:
    """The '## <title>' section (up to the next '## ' heading), heading included."""
    body = text() if body is None else body
    m = re.search(rf"^## {re.escape(title)}.*?(?=^## |\Z)", body, re.M | re.S)
    return m.group(0).strip() if m else ""


_STOP = {"the", "and", "for", "with", "university", "department", "group", "inc", "llc", "corp", "company", "team", "club",
         "of", "at", "texas", "remote", "part", "time", "intern", "engineer", "software", "research", "assistant",
         "current", "past", "employment", "never", "no", "not"}
_MONTHS = re.compile(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b|\b(19|20)\d{2}\b|\bto present\b|\bended\b", re.I)


def employer_names() -> list[str]:
    """Employers and organisations the profile names: the employer segment of each '### Employer — Role …'
    heading under Experience, and the organisations in the Current/Past bullets of Employment and affiliations."""
    body = text()
    names: list[str] = []

    def _add(piece: str) -> None:
        piece = re.sub(r"\s+", " ", piece).strip(" ,.;:")
        if len(piece) >= 4 and piece.lower() not in _STOP and not _MONTHS.search(piece):
            names.append(piece)

    for head in re.findall(r"^### (.+)$", section("Experience", body), re.M):
        employer = re.split(r"\s[—–-]\s", head)[0]
        for piece in re.split(r"\s*[,/]\s*", re.sub(r"\(([^)]*)\)", r", \1", employer)):
            _add(piece)
    aff = section("Employment and affiliations", body)
    for bullet in re.findall(r"^- (Current|Past):\s*(.+?)(?=^- |\Z)", aff, re.M | re.S):
        txt_ = re.sub(r"\([^)]*\)", " ", bullet[1])          # parentheticals describe the role, not the name
        for m in re.finditer(r"\b([A-Z][\w&.'-]*(?:\s+(?:of|&|and)?\s*[A-Z][\w&.'-]*){0,5})", txt_):
            _add(m.group(1))
    out, seen = [], set()
    for n in names:
        k = n.lower()
        if k not in seen:
            seen.add(k)
            out.append(n)
    return out


def mentions(question: str) -> list[str]:
    """Profile names that appear in the question (whole words, case-insensitive)."""
    q = " " + re.sub(r"[^a-z0-9&.' -]+", " ", (question or "").lower()) + " "
    hits = []
    for n in employer_names():
        k = n.lower()
        if len(k) >= 4 and re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", q):
            hits.append(n)
    return hits


def affiliations_brief() -> str:
    """What a yes/no question about employers, industries and relationships needs: the affiliations
    section plus one line per role from the Experience section."""
    body = text()
    roles = re.findall(r"^### (.+)$", section("Experience", body), re.M)
    out = [section("Employment and affiliations", body)]
    if roles:
        out.append("Roles (résumé):\n" + "\n".join(f"- {r}" for r in roles))
    return "\n\n".join(x for x in out if x)
