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
# A phone country-code picker that draws only a flag shows no text to compare (SmartRecruiters' 'Country code', CapTech
# 2026-10-21: the model recorded '+1', the read-back saw '' and blocked Next; the number itself carries the code).
PHONE_COUNTRY = re.compile(r"^\s*(country( calling)? code|phone country( code)?|dial(l?ing)? code|country/region code)\s*\*?\s*:?\s*$", re.I)
SKIP_VALUES = ("(set)", "No results were found.", "No results found", "No results", "No matches found", "No options")
# every credentials call records one decision with this source: what was typed before it on that page was the sign-in,
# sign-up or forgot-password form, not the application
ACCOUNT_SOURCE = "policy:may_create_accounts"
_DATE_PARTS = re.compile(r"month=(\d{1,2})(?:\s+day=\d{1,2})?\s+year=(\d{4})", re.I)
_DATEISH_LABEL = re.compile(r"\b(date|month|year|day)\b", re.I)


def _nw(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 2}


_DAY_PART = re.compile(r"\bday=(\d{1,2})\b", re.I)
_SETDATE = re.compile(r"\bmonth=\d{1,2}\b.*\byear=\d{4}|\byear=\d{4}\b.*\bmonth=\d{1,2}", re.I)


def _date_parts(value: str) -> dict:
    m = _DATE_PARTS.search(value or "")
    if m:
        out = {"month": f"{int(m.group(1)):02d}", "year": m.group(2)}
        dm = _DAY_PART.search(value or "")
        if dm:
            out["day"] = f"{int(dm.group(1)):02d}"
        return out
    p = fill.parse_date(value or "")
    return {k: v for k, v in p.items() if k in ("month", "year")}


def _as_date(value: str) -> dict | None:
    """A date needs at least a month and a year; '2026' alone or '6' alone is not one."""
    p = _date_parts(value)
    return p if p.get("month") and p.get("year") else None


def _truthy_checked(value: str) -> bool:
    return str(value or "").strip().lower() in ("checked", "pressed", "true", "yes", "on", "1")


# A phone-number country picker shows the dialing code of the country picked: Greenhouse's "Country*" box beside
# Phone reads "+1" once United States is chosen (Epic Games, 2026-10-19).
_DIAL_CODES = {
    "1": ("united states", "united states of america", "usa", "us", "u s", "u s a", "america", "canada"),
    "44": ("united kingdom", "uk", "great britain", "england"),
    "91": ("india",),
}


def dial_code_matches(decision: str, page: str) -> bool:
    """`page` is a bare dialing code ("+1", with or without a flag in front) of the country `decision` names."""
    m = re.fullmatch(r"[^\w+]*\+\s*(\d{1,4})\s*", str(page or ""))
    if not m:
        return False
    nd = answers.norm(decision)
    return bool(nd) and any(nd == n or nd.startswith(n + " ") for n in _DIAL_CODES.get(m.group(1), ()))


def _expect_checked(value: str, item: dict) -> str:
    """A checkbox or radio decision recorded as the option's own wording ("No, I do not have a disability and have
    not had one in the past", "Yes, I have read and consent to the terms") means that option is ticked: T-Mobile and
    Axiom Space were blocked with "shows 'checked' but decision was '<its label>'" (2026-10-19)."""
    v = str(value or "")
    lab = str(item.get("label") or "")
    # the option's own wording first: Ashby's Yes / No buttons record "No" on the button labelled No, which shows
    # 'pressed' (Crusoe, 2026-10-21: "field No [e16] shows 'pressed' but decision was 'No'")
    if lab and v.strip() and answers.norm(lab) == answers.norm(v):
        return "checked"
    if _truthy_checked(v) or answers.norm(v) in ("", "unchecked", "false", "off", "0", "no"):
        return v
    if lab and values_equal(v, lab, "choice"):
        return "checked"
    return v


def values_equal(decision: str, page: str, kind: str, label: str = "") -> bool:
    """kind: text | choice | checkbox | toggle | date. `label` is the control's label, which a choose
    read-back often prefixes ("Degree* Bachelors")."""
    d = str(decision if decision is not None else "")
    p = str(page if page is not None else "")
    if kind in ("checkbox", "toggle"):
        return _truthy_checked(d) == _truthy_checked(p)
    if _SETDATE.search(d):
        kind = "date"       # a set_date decision ("day=15 month=5 year=2028") read against one of its boxes
    if kind == "date":
        dd, pp = _date_parts(d), _date_parts(p)
        if label and re.fullmatch(r"\s*day\s*\*?\s*", label, re.I):
            # Leidos (2026-10-20): the Day box showed '15' for 'day=15 month=5 year=2028' and verification failed on it
            return bool(dd.get("day")) and p.strip().isdigit() and int(p.strip()) == int(dd["day"])
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
    # a number box normalizes what was typed ("4.0" shows as "4" on Sierra's GPA field, 2026-10-18)
    try:
        if float(d.strip()) == float(p.strip()):
            return True
    except ValueError:
        pass
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
    # a picked term that holds the decided month ('Spring 2028' for 'May 2028', CapTech 2026-10-21)
    from .agent.tools import date_option_covers
    if kind == "choice" and date_option_covers(d, p):
        return True
    # a typed value the box truncated at its cap, or a long text the outline cut off
    if len(np_) >= 40 and (nd.startswith(np_) or np_.startswith(nd)):
        return True
    # a state picker shows the code for the stored name ("TX" for "Texas", onsemi's Oracle address, 2026-10-19)
    if _STATE_CODES.get(nd) == np_ or _STATE_CODES.get(np_) == nd:
        return True
    # a decision that holds his whole preference chain matches the one entry the portal shows
    if ";" in d:
        parts = [x.strip() for x in d.split(";") if x.strip()]
        if len(parts) > 1 and any(values_equal(x, p, kind, label) for x in parts):
            return True
    return False


_STATES = {"alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar", "california": "ca", "colorado": "co",
           "connecticut": "ct", "delaware": "de", "district of columbia": "dc", "florida": "fl", "georgia": "ga", "hawaii": "hi",
           "idaho": "id", "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks", "kentucky": "ky", "louisiana": "la",
           "maine": "me", "maryland": "md", "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
           "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv", "new hampshire": "nh", "new jersey": "nj",
           "new mexico": "nm", "new york": "ny", "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
           "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc", "south dakota": "sd",
           "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt", "virginia": "va", "washington": "wa",
           "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy", "puerto rico": "pr"}
_STATE_CODES = {**_STATES, **{v: k for k, v in _STATES.items()}}


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


_FIELD_NOISE = {"selected", "items", "item", "choose", "control", "adds", "another", "select", "please", "your", "you", "the", "this",
                "that", "and", "for", "with", "are", "have", "what", "which", "required", "optional", "enter", "field", "box"}


def _stems(label: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-z0-9]+", (label or "").lower()) if len(w) >= 3 and w not in _FIELD_NOISE}


def _same_field(decision_label: str, item_label: str) -> bool:
    """Whether a decision's label and a control's label can name the same field: some word in common (on 5-letter
    stems, so 'authorized' meets 'authorization'). Either label empty: no evidence against, so yes."""
    a, b = _stems(decision_label), _stems(item_label)
    if not a or not b or a & b:
        return True
    # a Workday date part ("Month", "Year", "Day") belongs to a date decision ("From date", "Start date", "Graduation")
    return bool(re.match(r"\s*(month|year|day)\b", item_label or "", re.I)
                and re.search(r"date|from|\bto\b|start|end|graduat|month|year|day|since|until", decision_label or "", re.I))


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
    # The account forms come before the application on the same page number: SWBC's forgot-password 'Email Address' paired
    # by its words with the referrer box 'What's their name or email address?', and the run typed his own email there to
    # match it (2026-10-23). Decisions made before the last credentials call on its page are left out.
    last_acct = max((k for k, d in enumerate(decisions) if str(d.get("source") or "") == ACCOUNT_SOURCE), default=-1)
    if last_acct >= 0:
        acct_page = int(decisions[last_acct].get("page") or 0)
        decisions = [d for k, d in enumerate(decisions) if k > last_acct or int(d.get("page") or 0) != acct_page]
    latest: dict[str, dict] = {}
    # In a radio group only the newest pick counts: ticking No supersedes an earlier Yes on the sibling option
    # (American Family, 2026-10-18), so older decisions on other options of the same group are not compared.
    newest_in_group: dict[str, str] = {}
    for d in decisions:
        r = d.get("ref") or ""
        it = snap.refs.get(r) if r else None
        # a Yes/No toggle-button pair (Ashby) is a radio group too: pressing Yes supersedes an earlier No (Sentry, 2026-10-19)
        if it and ((it.get("type") or "").lower() == "radio" or it.get("tag") == "toggle") and it.get("group") \
                and (not page_no or int(d.get("page") or 0) == page_no):
            newest_in_group[str(it.get("group"))] = r
    for d in decisions:
        ref = d.get("ref") or ""
        same_page = not page_no or int(d.get("page") or 0) == page_no
        # Refs are only meaningful on the page they were made on: Workday swaps steps in place and hands the same
        # e27 to School on page 3 that First Name had on page 1 (ABA, 2026-10-18). Other pages' decisions never pair.
        if not same_page:
            continue
        item = snap.refs.get(ref) if ref else None
        # a toggle or radio is named by its option ('Yes') and its question (the group): a decision labelled with the
        # question is the same field (Walrus' 'Authorized to work' on the 'Yes' button was never compared, 2026-10-23)
        if item is not None and not _same_field(str(d.get("field_label") or ""), str(item.get("label") or item.get("group") or "")) \
                and not (item.get("group") and _same_field(str(d.get("field_label") or ""), str(item.get("group")))):
            # Workday re-renders a block after a prompt pick and the ref lands on another control (Moderna, 2026-10-19:
            # e189 was the GPA box, then the School prompt, and the GPA decision blocked the page): pair by label instead.
            item = _match_by_label(d, snap) if page_no else None
        if item is None and page_no:
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
        if ((item.get("type") or "").lower() == "radio" or item.get("tag") == "toggle") and item.get("group") \
                and newest_in_group.get(str(item.get("group"))) not in (None, key):
            continue          # superseded by a newer pick on another option of the same radio group
        kind = _kind_for(item, str(val))
        page_value = _page_value(item)
        label = (item.get("label") or d.get("field_label") or "")
        if not str(page_value or "").strip() and PHONE_COUNTRY.match(label):
            continue
        if kind in ("checkbox", "toggle"):
            val = _expect_checked(str(val), item)
        if kind != "checkbox" and dial_code_matches(str(val), page_value):
            continue
        if item.get("selected_items") and "; " in page_value and any(
                values_equal(str(val), chip, kind, label) for chip in page_value.split("; ")):
            continue          # a multi-select keeps every pick as a chip: the decision is one of them
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
            # the live read's shown pick counts even when the snapshot had none: SmartRecruiters' City box was emptied
            # while its host kept 'Sugar Land, TX, US', and the read-back called it '' twelve pages running (ServiceNow,
            # 2026-10-21)
            if info.get("shown"):
                item["shown"] = info.get("shown")
            out[ref] = str(item.get("shown") or item.get("value") or "")
    return out
