"""Phase 0: exercise every disputed OpenAI API behaviour once, before any feature work.

  python scripts/phase0_api_checks.py [--models gpt-6.1-sol,gpt-6-astra,gpt-6-luna]

Checks, per model:
  1. the `computer` tool is accepted alongside function tools on the Responses API
  2. store=False + include=["reasoning.encrypted_content"] returns encrypted reasoning items
  3. a `configuration_update` item changing reasoning effort is accepted mid-conversation
  4. the classifier model returns strict JSON schema output with effort "none"
  5. computer_call_output with `detail: "original"` is accepted (needs the model to issue a computer_call)
Results are printed as JSON and stored in meta['phase0_api_checks'] so the run log can cite them."""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from goat import db  # noqa: E402
from goat.config import SETTINGS, assert_allowed  # noqa: E402

def _png(w: int = 64, h: int = 64) -> str:
    """A valid grey PNG built with the stdlib (the API rejects malformed images)."""
    import struct
    import zlib

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes([200, 200, 200] * w) for _ in range(h))
    sig = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
    data = sig + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    return base64.b64encode(data).decode()


TINY_PNG = _png()


def check(client, model: str) -> dict:
    out: dict = {"model": model}
    tools = [{"type": "computer"}, {"type": "function", "name": "noop", "strict": True, "description": "does nothing",
                                     "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}]
    # 1 + 2
    try:
        r = client.responses.create(model=model, reasoning={"effort": "low"}, tools=tools, store=False,
                                    include=["reasoning.encrypted_content"], parallel_tool_calls=False,
                                    safety_identifier=SETTINGS.safety_identifier(),
                                    input=[{"role": "user", "content": [{"type": "input_text", "text":
                                            "Take a screenshot of the computer using the computer tool. Do nothing else."}]}])
        out["computer_tool_accepted"] = True
        out["served_model"] = r.model
        out["encrypted_reasoning_returned"] = any(getattr(o, "type", "") == "reasoning" and getattr(o, "encrypted_content", None) for o in r.output)
        cc = next((o for o in r.output if getattr(o, "type", "") == "computer_call"), None)
        out["issued_computer_call"] = cc is not None
        items = [{"role": "user", "content": [{"type": "input_text", "text": "Take a screenshot of the computer using the computer tool. Do nothing else."}]}]
        items += [o.model_dump(exclude_none=True) for o in r.output]
        # 5
        if cc is not None:
            out["pending_safety_checks"] = [c.code for c in (cc.pending_safety_checks or [])]
            items.append({"type": "computer_call_output", "call_id": cc.call_id,
                          "output": {"type": "computer_screenshot", "image_url": "data:image/png;base64," + TINY_PNG, "detail": "original"}})
            try:
                r2 = client.responses.create(model=model, reasoning={"effort": "low"}, tools=tools, store=False,
                                             include=["reasoning.encrypted_content"], input=items,
                                             safety_identifier=SETTINGS.safety_identifier())
                out["screenshot_detail_original_accepted"] = True
                items += [o.model_dump(exclude_none=True) for o in r2.output]
            except Exception as e:
                out["screenshot_detail_original_accepted"] = False
                out["detail_error"] = str(e)[:300]
        # 3
        items.append({"type": "configuration_update", "reasoning": {"effort": "medium"}})
        items.append({"role": "user", "content": [{"type": "input_text", "text": "Reply with the single word OK."}]})
        try:
            r3 = client.responses.create(model=model, reasoning={"effort": "low"}, tools=tools, store=False,
                                         include=["reasoning.encrypted_content"], input=items,
                                         safety_identifier=SETTINGS.safety_identifier())
            out["configuration_update_accepted"] = True
            out["effort_after_update"] = getattr(getattr(r3, "reasoning", None), "effort", None)
        except Exception as e:
            out["configuration_update_accepted"] = False
            out["configuration_update_error"] = str(e)[:300]
    except Exception as e:
        out["computer_tool_accepted"] = False
        out["error"] = str(e)[:400]
    return out


def check_classifier(client, model: str) -> dict:
    schema = {"type": "object", "properties": {"label": {"type": "string", "enum": ["form", "login", "other"]}, "confidence": {"type": "number"}},
              "required": ["label", "confidence"], "additionalProperties": False}
    try:
        r = client.responses.create(model=model, reasoning={"effort": "none"}, store=False,
                                    input="Page outline: textbox First Name, textbox Email, button Submit Application. Label it.",
                                    text={"format": {"type": "json_schema", "name": "page", "schema": schema, "strict": True}})
        return {"model": model, "structured_output_effort_none": True, "out": json.loads(r.output_text), "served_model": r.model}
    except Exception as e:
        return {"model": model, "structured_output_effort_none": False, "error": str(e)[:400]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="")
    a = ap.parse_args()
    if not SETTINGS.openai_api_key:
        print("OPENAI_API_KEY is empty in .env; nothing to check yet.")
        return 2
    from openai import OpenAI
    client = OpenAI(api_key=SETTINGS.openai_api_key)
    models = sorted({assert_allowed(m) for m in (a.models.split(",") if a.models else [SETTINGS.models.executor, SETTINGS.models.advisor]) if m})
    results = {"executor_checks": [check(client, m) for m in models],
               "classifier": check_classifier(client, assert_allowed(SETTINGS.models.classifier))}
    print(json.dumps(results, indent=2, default=str))
    db.connect()
    db.meta_set("phase0_api_checks", results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
