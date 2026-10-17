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


def affiliations_brief() -> str:
    """What a yes/no question about employers, industries and relationships needs: the affiliations
    section plus one line per role from the Experience section."""
    body = text()
    roles = re.findall(r"^### (.+)$", section("Experience", body), re.M)
    out = [section("Employment and affiliations", body)]
    if roles:
        out.append("Roles (résumé):\n" + "\n".join(f"- {r}" for r in roles))
    return "\n\n".join(x for x in out if x)
