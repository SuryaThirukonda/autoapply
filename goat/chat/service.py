"""ChatService: the asyncio task that owns the ChatAgent. Messages arrive from any thread (the cloud link
applies a `chat` command in its worker thread) through `enqueue`, are handled one at a time on the main loop
(where Playwright lives), and answered with `chat:reply` events."""
from __future__ import annotations

import asyncio
import logging

from .agent import ChatAgent, ChatTurn

log = logging.getLogger(__name__)

_loop: asyncio.AbstractEventLoop | None = None
_queue: asyncio.Queue | None = None


def enqueue(payload: dict, source: str = "telegram") -> dict:
    """Thread-safe: hand a message to the running service. Returns {ok, queued} or a refusal when no service runs."""
    if _loop is None or _queue is None:
        return {"ok": False, "reason": "chat_offline", "message": "The chat agent is not running in this process."}
    turn = ChatTurn(text=str(payload.get("text") or "").strip(), source=source,
                    message_id=int(payload["message_id"]) if payload.get("message_id") is not None else None,
                    reply_to_job_id=int(payload["reply_to_job_id"]) if payload.get("reply_to_job_id") else None)
    if not turn.text:
        return {"ok": False, "reason": "empty", "message": "Empty message."}
    _loop.call_soon_threadsafe(_queue.put_nowait, turn)
    return {"ok": True, "queued": True}


class ChatService:
    def __init__(self, worker_control=None):
        self.agent = ChatAgent(worker_control=worker_control)

    async def run(self, stop: asyncio.Event) -> None:
        global _loop, _queue
        _loop = asyncio.get_running_loop()
        _queue = asyncio.Queue()
        log.info("chat agent: ready")
        try:
            while not stop.is_set():
                try:
                    turn = await asyncio.wait_for(_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                try:
                    await asyncio.wait_for(self.agent.handle(turn), timeout=240)
                except Exception as e:
                    log.exception("chat agent: turn failed")
                    from .. import db
                    db.event(turn.reply_to_job_id, "chat:reply", {"text": f"Something broke while handling that ({type(e).__name__}). Try again or check the log.",
                                                                  "in_reply_to": turn.message_id, "source": turn.source})
        finally:
            _loop, _queue = None, None
