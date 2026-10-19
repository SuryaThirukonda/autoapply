"""Surya's knowledge base as one text block for the resolver: every stored answer with the wordings it
was asked under and its note, the company's own overrides, and the profile. Built once per change of the
answer store (cheap to check), so a run reuses the same text and the model's prompt cache."""
from __future__ import annotations

from .. import answers, db
from .. import profile as profile_mod

_cache: dict[tuple, str] = {}

SKIP_PREFIXES = ("story.", "always_hold.")


def _version() -> tuple:
    con = db.connect()
    r = con.execute("SELECT COALESCE(MAX(id), 0) AS a FROM answers").fetchone()
    q = con.execute("SELECT COALESCE(MAX(id), 0) AS q FROM question_aliases").fetchone()
    return (r["a"], q["q"])


def knowledge_text(company: str = "", ats: str = "", profile_chars: int = 30000) -> str:
    key = (_version(), answers.norm(company), (ats or "").lower(), profile_chars)
    if key in _cache:
        return _cache[key]
    con = db.connect()
    asked: dict[str, list[str]] = {}
    for r in con.execute("SELECT key, alias FROM question_aliases WHERE kind='exact' AND approved=1 ORDER BY LENGTH(alias)"):
        lst = asked.setdefault(r["key"], [])
        if len(lst) < 3:
            lst.append(r["alias"][:140])
    lines = ["ANSWER STORE (key | asked as | answer | note). An answer with ';' is an ordered preference list.",
             "An answer shown as (none) means Surya has not decided it: never treat that as an answer."]
    for r in con.execute("SELECT key, answer, answer_type, note FROM answers WHERE superseded_by IS NULL AND approved=1 "
                         "AND scope='global' ORDER BY key"):
        if r["key"].startswith(SKIP_PREFIXES):
            continue
        atype = r["answer_type"] or ""
        if r["answer"] is None and atype != "blank":
            val = "(none)"
        elif atype == "blank":
            val = "(leave empty)"
        elif atype.startswith("conditional:"):
            val = f"{str(r['answer'])[:160]} [depends on {atype.split(':', 1)[1]}]"
        elif str(r["answer"]).strip().lower() in ("none", "n/a", "na", "none."):
            # his answer IS "None" (nothing applies); shown bare it read as a missing value and Aptiv asked him the same
            # travel question twice (2026-10-19)
            val = f'"{str(r["answer"]).strip()}" (his answer: nothing applies)'
        else:
            val = str(r["answer"]).replace("\n", " ")[:220]
        wording = "; ".join(f'"{a}"' for a in asked.get(r["key"], []))
        note = (r["note"] or "").replace("\n", " ")[:140]
        lines.append(f"- {r['key']} | {wording or '-'} | {val}" + (f" | {note}" if note else ""))
    comp = []
    for r in con.execute("SELECT key, answer, note FROM answers WHERE superseded_by IS NULL AND approved=1 "
                         "AND ((scope='company' AND scope_value=?) OR (scope='ats' AND scope_value=?))",
                         (answers.norm(company), (ats or "").lower())):
        val = "(none: ask Surya)" if r["answer"] is None else str(r["answer"])[:220]
        comp.append(f"- {r['key']} | {val}" + (f" | {(r['note'] or '')[:140]}" if r["note"] else ""))
    if comp:
        lines += ["", f"FOR {company or ats} ONLY (these override the store):"] + comp
    prof = profile_mod.text(profile_chars)
    if prof:
        lines += ["", "PROFILE (everything known about Surya):", prof]
    text = "\n".join(lines)
    if len(_cache) > 8:
        _cache.clear()
    _cache[key] = text
    return text
