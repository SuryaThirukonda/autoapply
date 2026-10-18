"""Read-back diff: every decision the run recorded is compared with what the page shows now.

The gate used to check only that a sourced decision EXISTS for each value on the page; a picker that
reported "set" while the control kept another value, or a later step that cleared a typed value, went
unnoticed (roadmap 2026-10-18 item 1). `diff()` pairs each decision with the control it was made on
(by ref, else by label words on the same page) and names every mismatch; the gate refuses to submit on
one, and the worker reports them after each page as advice to the model."""
from __future__ import annotations

import re

from . import answers
from .browser import fill, js
from .browser.snapshot import Snapshot

SKIP_SOURCES = ("cleared", "judgment")
SKIP_VALUES = ("(set)",)
_DATE_PARTS = re.compile(r"month=(\d{1,2})(?:\s+day=\d{1,2})?\s+year=(\d{4})", re.I)
_DATEISH_LABEL = re.compile(r"\b(date|month|year|day)\b", re.I)


def _nw(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 2}


def _date_parts(value: str) -> dict:
    m = _DATE_PARTS.search(value or "")
    if m:
        return {"month": f"{int(m.group(1)):02d}", "year": m.group(2)}
    p = fill.parse_date(value or "")
    return {k: v for k, v in p.items() if k in ("month", "year")}


def _as_date(value: str) -> dict | None:
    """A date needs at least a month and a year; '2026' alone or '6' alone is not one."""
    p = _date_parts(value)
    return p if p.get("month") and p.get("year") else None


def _truthy_checked(value: str) -> bool:
    return str(value or "").strip().lower() in ("checked", "pressed", "true", "yes", "on", "1")


def values_equal(decision: str, page: str, kind: str, label: str = "") -> bool:
    """kind: text | choice | checkbox | toggle | date. `label` is the control's label, which a choose
    read-back often prefixes ("Degree* Bachelors")."""
    d = str(decision if decision is not None else "")
    p = str(page if page is not None else "")
    if kind in ("checkbox", "toggle"):
        return _truthy_checked(d) == _truthy_checked(p)
    if kind == "date":
        dd, pp = _date_parts(d), _date_parts(p)
        if label and re.fullmatch(r"\s*month\s*\*?\s*", label, re.I):
            return bool(dd.get("month")) and p.strip().zfill(2) == dd["month"]
        if label and re.fullmatch(r"\s*year\s*\*?\s*", label, re.I):
            return bool(dd.get("year")) and p.strip() == dd["year"]
        if not pp.get("month") and not pp.get("year"):
            return False
        return all(dd.get(k) == pp.get(k) for k in ("month", "year") if dd.get(k) and pp.get(k))
    nd, np_ = answers.norm(d), answers.norm(p)
    if nd == np_:
        return True
    if not nd or not np_:
        return False
    # a choose read-back echoes the field label in front of the value ("Degree* Bachelors")
    nl = answers.norm(label)
    if nl and nd.startswith(nl) and nd[len(nl):].strip() == np_:
        return True
    if nd.endswith(" " + np_) or np_.endswith(" " + nd):
        return True
    if fill._chip_matches(p, [d]) or fill._chip_matches(d, [p]):
        return True
    pick = fill.best_option([{"text": p}], [d])
    if pick is not None:
        return True
    # a typed value the box truncated at its cap, or a long text the outline cut off
    if len(np_) >= 40 and (nd.startswith(np_) or np_.startswith(nd)):
        return True
    return False


def _kind_for(item: dict, decision_value: str) -> str:
    typ = (item.get("type") or "").lower()
    if typ in ("checkbox", "radio"):
        return "checkbox"
    if item.get("tag") == "toggle":
        return "toggle"
    if _as_date(decision_value) and (_DATEISH_LABEL.search(item.get("label") or "") or _DATE_PARTS.search(decision_value)):
        return "date"
    if item.get("tag") in ("select", "dropdown") or item.get("combo"):
        return "choice"
    return "text"


def _page_value(item: dict) -> str:
    typ = (item.get("type") or "").lower()
    if typ in ("checkbox", "radio"):
        return "checked" if item.get("checked") else ""
    if item.get("tag") == "toggle":
        return "pressed" if item.get("pressed") else ""
    return str(item.get("shown") or item.get("value") or "")


def _match_by_label(decision: dict, snap: Snapshot) -> dict | None:
    """A decision whose ref is gone (the portal re-rendered) still pairs with the one control whose label
    words contain, or are contained in, the decision's label words. Ambiguity means no match."""
    lw = _nw(decision.get("field_label") or "")
    if not lw:
        return None
    exact = answers.norm(decision.get("field_label") or "")
    hits, same = [], []
    for it in snap.items:
        if it.get("type") in ("file", "password", "hidden") or it.get("tag") in ("button", "a"):
            continue
        il = it.get("label") or ""
        if answers.norm(il) == exact:
            same.append(it)
        iw = _nw(il)
        if iw and (lw <= iw or iw <= lw):
            hits.append(it)
    if len(same) == 1:
        return same[0]
    return hits[0] if len(hits) == 1 else None


def diff(decisions: list[dict], snap: Snapshot, page_no: int = 0) -> list[str]:
    """Mismatch messages, one per control whose NEWEST decision differs from what it shows now (decisions
    come oldest first; a control re-set later in the run is judged by its last decision)."""
    latest: dict[str, dict] = {}
    for d in decisions:
        ref = d.get("ref") or ""
        item = snap.refs.get(ref) if ref else None
        if item is None and page_no and int(d.get("page") or 0) == page_no:
            item = _match_by_label(d, snap)
        if item is None:
            continue
        latest[item.get("ref") or ref] = {**d, "_item": item}
    out: list[str] = []
    for key, d in latest.items():
        src = str(d.get("source") or "")
        val = d.get("value")
        if src in SKIP_SOURCES or src.startswith("resume") or val is None or str(val) in SKIP_VALUES:
            continue
        item = d["_item"]
        if item.get("type") in ("file", "password", "hidden") or item.get("tag") in ("button", "a"):
            continue
        kind = _kind_for(item, str(val))
        page_value = _page_value(item)
        label = (item.get("label") or d.get("field_label") or "")
        if not values_equal(str(val), page_value, kind, label):
            out.append(f"field {label[:60]} [{item.get('ref', key)}] shows {page_value[:60]!r} but decision was {str(val)[:60]!r}")
    return out


async def refresh_values(ex, snap: Snapshot, refs: list[str] | None = None) -> dict[str, str]:
    """Current DOM values for the given refs (default: every ref the snapshot knows), read without
    scrolling the page, and folded back into the snapshot so `diff` sees the live state."""
    want = list(refs) if refs is not None else list(snap.refs)
    if not want:
        return {}
    try:
        raw = await ex.eval(js.JS_READ_REFS, want)
    except Exception:
        return {}
    out: dict[str, str] = {}
    for ref, info in (raw or {}).items() if isinstance(raw, dict) else []:
        item = snap.refs.get(ref)
        if not isinstance(info, dict) or item is None:
            continue
        if "checked" in info and item.get("type") in ("checkbox", "radio"):
            item["checked"] = bool(info["checked"])
            out[ref] = "checked" if info["checked"] else ""
        elif "pressed" in info and item.get("tag") == "toggle":
            item["pressed"] = bool(info["pressed"])
            out[ref] = "pressed" if info["pressed"] else ""
        else:
            item["value"] = info.get("value", item.get("value"))
            if info.get("shown") is not None and item.get("shown"):
                item["shown"] = info.get("shown")
            out[ref] = str(item.get("shown") or item.get("value") or "")
    return out
