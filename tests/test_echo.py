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
