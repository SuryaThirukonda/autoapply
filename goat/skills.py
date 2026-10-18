"""Skills: Markdown files under skills/ (and data/skills/ for local-only ones) with a small front matter:

    ---
    name: workday
    description: one line, shown in the skills index
    portals: [workday]          # optional: loaded into the filling agent's prompt for these portals
    agents: [filler, chat]      # which agent reads it (default both)
    triggers: [sign in, code]   # optional: words that pull it into the chat agent's prompt
    ---
    body in plain Markdown

The filling agent gets the bodies of every skill whose portals include the job's portal (plus skills with
no portal list that name the filler), under a SKILLS block of its cached prefix. The chat agent gets the
index (name + description) always and the bodies of the skills a message triggers, and can read any skill
by name. Skills hold procedures, never facts about Surya (those live in config/ and the answer store)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import SETTINGS

ROOT = Path(__file__).resolve().parent.parent
DIRS = [ROOT / "skills", SETTINGS.db_path.parent / "skills"]   # repo skills, then local-only ones under data/
_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


@dataclass
class Skill:
    name: str
    description: str
    body: str
    portals: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=lambda: ["filler", "chat"])
    triggers: list[str] = field(default_factory=list)
    path: Path | None = None

    def matches_text(self, text: str) -> bool:
        low = (text or "").lower()
        return any(t.lower() in low for t in self.triggers) or self.name.lower() in low


def _parse_front_matter(raw: str) -> tuple[dict, str]:
    m = _FM.match(raw)
    if not m:
        return {}, raw
    meta: dict = {}
    try:
        import yaml  # type: ignore
        meta = yaml.safe_load(m.group(1)) or {}
    except Exception:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                v = v.strip()
                if v.startswith("[") and v.endswith("]"):
                    meta[k.strip()] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
                else:
                    meta[k.strip()] = v.strip("'\"")
    return (meta if isinstance(meta, dict) else {}), raw[m.end():]


def load_all() -> list[Skill]:
    out: list[Skill] = []
    seen: set[str] = set()
    for d in DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            try:
                meta, body = _parse_front_matter(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            name = str(meta.get("name") or p.stem)
            if name in seen:
                continue
            seen.add(name)
            out.append(Skill(
                name=name,
                description=str(meta.get("description") or "").strip(),
                body=body.strip(),
                portals=[str(x).lower() for x in (meta.get("portals") or [])],
                agents=[str(x).lower() for x in (meta.get("agents") or ["filler", "chat"])],
                triggers=[str(x) for x in (meta.get("triggers") or [])],
                path=p,
            ))
    return out


def get(name: str) -> Skill | None:
    low = (name or "").strip().lower()
    return next((s for s in load_all() if s.name.lower() == low), None)


def index(agent: str = "chat") -> str:
    rows = [f"- {s.name}: {s.description}" for s in load_all() if agent in s.agents]
    return "SKILLS (procedures; read one with read_skill):\n" + "\n".join(rows) if rows else ""


def for_filler(portal: str, max_chars: int = 7000) -> str:
    """The bodies the filling agent carries for this portal: portal-specific skills first, then general ones."""
    portal = (portal or "").lower()
    picked = [s for s in load_all() if "filler" in s.agents and (portal in s.portals or (not s.portals))]
    picked.sort(key=lambda s: (0 if portal in s.portals else 1, s.name))
    parts, used = [], 0
    for s in picked:
        chunk = f"### {s.name}\n{s.body}\n"
        if used + len(chunk) > max_chars:
            break
        parts.append(chunk)
        used += len(chunk)
    return ("SKILLS (procedures for this portal; follow them before improvising):\n" + "\n".join(parts)) if parts else ""


def for_chat(text: str, max_chars: int = 4000) -> str:
    """Bodies of the skills a message triggers (the chat agent always has the index as well)."""
    parts, used = [], 0
    for s in load_all():
        if "chat" in s.agents and s.matches_text(text):
            chunk = f"### {s.name}\n{s.body}\n"
            if used + len(chunk) > max_chars:
                break
            parts.append(chunk)
            used += len(chunk)
    return "\n".join(parts)
