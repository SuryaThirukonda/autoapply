"""Hy-Vee (Workday, 2026-10-25): the capture after a click timed out twice and the run ended as 'error: Page.screenshot'.
The model is shown a blank image instead and the run goes on."""
import pytest

from goat.agent import loop
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures


@pytest.mark.asyncio
async def test_a_failed_capture_gives_the_model_a_blank_image(store):
    _, ctx = _dispatcher(_job("Hy-Vee", "H1"))

    async def boom():
        raise TimeoutError("Page.screenshot: Timeout 20000ms exceeded.")
    ctx.ex.screenshot_data_url = boom
    agent = loop.AgentLoop(ctx, client=object())
    out = await agent._screenshot_output("call_1", [])
    assert out["output"]["image_url"] == loop.BLANK_PNG_URL
    from goat import db
    assert db.connect().execute("SELECT 1 FROM events WHERE kind='screenshot:failed'").fetchone()
