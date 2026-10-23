"""Whether a value already on the page that equals one of his stored answers answers THIS question. His whole phone number
in an area-code box does not (Westinghouse), nor does a city alone where the question asks a name with its city and state
(SWBC's 'List the educational institution name, city, and state' held only 'Sugar Land, TX', 2026-10-23), nor his own
email where it asks for someone else's (SWBC's referrer box 'What's their name or email address?')."""
from __future__ import annotations

import re

from . import phone

_ASKS_NAME = re.compile(r"\bname\b", re.I)
_ASKS_PLACE = re.compile(r"\b(city|state|location)\b", re.I)
_PLACE_KEY = re.compile(r"(city|location|state)(_variants)?$|city_state_zip$", re.I)
# a question about another person: whoever referred him, an emergency contact, a reference
_OTHER_PERSON = re.compile(r"\b(their|referr(?:er|ing)|referred by|who referred|employee'?s? (?:name|email)|"
                           r"emergency contact|reference \d|references?'? (?:name|email|phone)|supervisor'?s? (?:name|email|phone))\b", re.I)


def misfit(label: str, value: str, key: str | None = None, local: bool = False) -> bool:
    """True when `value` (stored answer `key`) is no answer to the field labelled `label`; `local`: the field follows an
    area-code box."""
    if phone.misfit(label, value, local):
        return True
    if key and key.startswith("identity.") and _OTHER_PERSON.search(label or ""):
        return True
    return bool(key and "name" not in key and _PLACE_KEY.search(key) and _ASKS_NAME.search(label or "")
                and _ASKS_PLACE.search(label or ""))
