"""His phone number where a form asks for its parts in separate boxes. SuccessFactors asks Home/Country Code, Home/Area
Code and Home Phone #: Westinghouse's area-code box held his whole number, prefilled from his candidate profile, and the
value was taken as his answer because it equals his stored phone (2026-10-23)."""
from __future__ import annotations

import re

from . import answers

# a short label that ends in the part it asks for ('Home/Area Code', 'Country Code', 'Phone Ext.'); 'Phone Number
# (including area code)' and 'Phone with country code' ask for the whole number
_PART_END = re.compile(r"(?:^|[^a-z])(?P<part>area|country|dial(?:l?ing)?|city)[\s_/-]*code$|(?:^|[^a-z])ext(?:ension)?$", re.I)
_JOINED = re.compile(r"\b(?:with|incl\w*|and|plus|or)\b", re.I)
PHONE_LABEL = re.compile(r"\b(?:phone|telephone|mobile|cell|number)\b|#", re.I)
AREA_KEY = "identity.phone_area_code"
LOCAL_KEY = "identity.phone_local"


def digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def part_of(label: str) -> str:
    """'area', 'country' or 'ext' for a box that asks only that part of a phone number, else ''."""
    lab = re.sub(r"[\s*:#.]+$", "", re.sub(r"^[\s*]+", "", label or "")).strip()
    if not lab or len(lab.split()) > 4 or _JOINED.search(lab):
        return ""
    m = _PART_END.search(lab)
    if not m:
        return ""
    p = (m.group("part") or "ext").lower()
    return "country" if p.startswith(("country", "dial")) else "area" if p in ("area", "city") else "ext"


def follows_area_code(items: list[dict], ref: str | None) -> bool:
    """The phone-number box right after an area-code box: it takes the number without its area code."""
    if not ref:
        return False
    fields = [i for i in items or [] if i.get("tag") in ("input", "select", "dropdown")
              and (i.get("type") or "text") not in ("checkbox", "radio", "file", "hidden")]
    for k, i in enumerate(fields):
        if i.get("ref") == ref:
            label = str(i.get("label") or "")
            return bool(k and part_of(str(fields[k - 1].get("label") or "")) == "area" and PHONE_LABEL.search(label)
                        and not part_of(label))
    return False


def misfit(label: str, value: str, local: bool = False) -> bool:
    """A whole phone number in a box for one of its parts: an area or country code box, an extension, or the number box
    after an area-code box (seven digits there)."""
    d = digits(value)
    if part_of(label):
        return len(d) >= 7
    return local and len(d) >= 10


def part_answer(part: str):
    """His area code ('area') or his number without it ('local'), stored on first use from his phone number
    ('seed:derived': the fact check reads its note)."""
    key = AREA_KEY if part == "area" else LOCAL_KEY
    a = answers.get(key)
    if a is not None and a.has_value:
        return a
    whole = answers.get("identity.phone")
    d = digits(whole.answer)[-10:] if whole is not None and whole.has_value else ""
    if len(d) != 10:
        return None
    if part == "area":
        answers.save_answer(AREA_KEY, d[:3], answer_type="text", source="seed:derived",
                            note=f"the area code of his phone number {whole.answer}")
    else:
        answers.save_answer(LOCAL_KEY, f"{d[3:6]}-{d[6:]}", answer_type="text", source="seed:derived",
                            note=f"his phone number {whole.answer} without its area code, for a number box that follows an "
                                 "area-code box")
    return answers.get(key)
