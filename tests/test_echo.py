"""Echoing model output back into the next request: a computer_call always carries exactly one of
`action` / `actions` (a bare call made the API answer 400 and ended a T-Mobile run)."""
from openai.types.responses.response_computer_tool_call import ResponseComputerToolCall

from goat.agent.loop import echo_item


def _call(**kw) -> ResponseComputerToolCall:
    return ResponseComputerToolCall.model_validate({"id": "cu_1", "call_id": "call_1", "pending_safety_checks": [],
                                                    "status": "completed", "type": "computer_call", **kw})


def test_a_bare_call_is_echoed_as_a_screenshot_action():
    d = echo_item(_call())
    assert d["action"] == {"type": "screenshot"} and "actions" not in d


def test_one_of_action_or_actions_survives():
    single = echo_item(_call(action={"type": "click", "x": 3, "y": 4, "button": "left"}))
    assert single["action"]["type"] == "click" and "actions" not in single
    batch = echo_item(_call(actions=[{"type": "scroll", "x": 1, "y": 2, "scroll_x": 0, "scroll_y": 300}],
                            action={"type": "screenshot"}))
    assert batch["actions"][0]["type"] == "scroll" and "action" not in batch
    empty_batch = echo_item(_call(actions=[], action={"type": "type", "text": "hi"}))
    assert empty_batch["action"]["type"] == "type" and "actions" not in empty_batch


def test_other_items_pass_through():
    assert echo_item({"type": "function_call", "name": "snapshot", "arguments": "{}", "call_id": "c"})["name"] == "snapshot"


def test_a_bare_call_is_left_out_with_its_reasoning():
    """Stoke Space (2026-10-19): even rebuilt as a screenshot action, the bare call was refused ("exactly one of `action`
    or `actions`"); it is now left out of the replay, together with the reasoning item that produced it."""
    from goat.agent.loop import echo_items, is_bare_computer_call
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": []}
    fc = {"type": "function_call", "name": "snapshot", "arguments": "{}", "call_id": "c"}
    assert is_bare_computer_call(_call()) and not is_bare_computer_call(_call(action={"type": "screenshot"}))
    out = echo_items([fc, reasoning, _call()])
    assert out == [fc]
    out = echo_items([reasoning, _call(action={"type": "click", "x": 1, "y": 2, "button": "left"})])
    assert [o["type"] for o in out] == ["reasoning", "computer_call"]


def test_location_wordings_map_to_the_city_picker():
    """IXL (2026-10-19): 'Your Location (Search by City)' has to be picked from the suggestions, not typed."""
    from goat.adapters import GENERIC_FIELD_MAP
    def rule(label):
        return next(((k, how) for rx, k, how in GENERIC_FIELD_MAP if rx.search(label)), None)
    for label in ("Your Location (Search by City)", "Location (City)", "Current location", "Location*", "Where are you located?",
                  "Your location"):
        assert rule(label) == ("identity.location_city", "choose"), label
    assert rule("Preferred location") != ("identity.location_city", "choose")
    assert rule("City") == ("identity.city", "text")
