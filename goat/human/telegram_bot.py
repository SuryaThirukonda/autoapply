"""The Telegram channel: owner-only aiogram bot (long polling). Questions go out as ONE message
per batch (forced reply, or inline buttons for a single multiple-choice question); replies and
button taps flow into questions.BROKER, which saves the answer, releases every held job on that
key in one transaction and wakes a waiting worker. Also /remember, /status and plain
notifications. Nothing that looks like a password or a code is ever logged or echoed."""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (BufferedInputFile, CallbackQuery, ForceReply, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from .. import answers, db
from ..config import SETTINGS
from . import questions

log = logging.getLogger("goat.telegram")

CAPTION_MAX = 1024       # photo caption limit
TEXT_MAX = 4096          # message limit
INLINE_MAX_OPTIONS = 8
RESTART_DELAY_S = 10
PROPOSAL_TTL_S = 60 * 60

CHANNEL: "TelegramChannel | None" = None


def _sensitive(key: str) -> bool:
    k = (key or "").lower()
    return any(s in k for s in db.REDACT_KEYS)


def _shown(key: str, answer: str | None) -> str:
    """Answer text for an echo back to the owner; hidden when the key looks secret."""
    if answer is None:
        return "(none)"
    return "[hidden]" if _sensitive(key) else str(answer)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_questions(job_id: int, qs: list[dict]) -> str:
    lines = [f"Job #{job_id} — {len(qs)} question(s). Reply to this message with your answers, "
             f"one per line, in order (or reply 'hold')."]
    for i, q in enumerate(qs, 1):
        line = f"{i}) {q.get('question') or q.get('key')}"
        opts = q.get("options") or []
        if opts:
            line += " [options: " + " | ".join(str(o) for o in opts) + "]"
        lines.append(line)
    return "\n".join(lines)


class TelegramChannel:
    def __init__(self, token: str | None = None, owner_chat_id: int | None = None):
        self.token = (SETTINGS.human.telegram_token if token is None else token) or ""
        self.owner_chat_id = int(SETTINGS.human.telegram_owner_chat_id if owner_chat_id is None else owner_chat_id)
        self.bot: Bot | None = None
        self._proposals: dict[str, dict] = {}   # /remember token -> {key, answer, alias, message_id, ts}
        self._started = False

    @property
    def enabled(self) -> bool:
        return bool(self.token) and self.owner_chat_id != 0

    def _is_owner(self, user) -> bool:
        return user is not None and int(user.id) == self.owner_chat_id

    # ------------------------------------------------------------------ outbound
    async def _send(self, text: str, png: bytes | None = None, reply_markup=None, parse_mode=None) -> Message | None:
        if self.bot is None:
            log.warning("telegram: bot not running; dropped a message")
            return None
        try:
            if png:
                try:
                    return await self.bot.send_photo(
                        self.owner_chat_id, BufferedInputFile(png, filename="page.png"),
                        caption=_clip(text, CAPTION_MAX), reply_markup=reply_markup, parse_mode=parse_mode)
                except Exception as e:  # too large / bad image: fall back to text
                    log.warning("telegram: photo send failed (%s); sending text only", type(e).__name__)
            return await self.bot.send_message(self.owner_chat_id, _clip(text, TEXT_MAX),
                                               reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception as e:
            log.warning("telegram: send failed: %s", type(e).__name__)
            return None

    async def send_questions(self, job_id: int, qs: list[dict], screenshot_png: bytes | None) -> list[int | None]:
        """ONE message per batch; every question gets that message id back."""
        if not qs:
            return []
        text = format_questions(job_id, qs)
        markup = ForceReply(selective=True, input_field_placeholder="Your answer(s), one per line")
        if len(qs) == 1 and qs[0].get("options") and len(qs[0]["options"]) <= INLINE_MAX_OPTIONS:
            pid = qs[0].get("id")
            rows = [[InlineKeyboardButton(text=_clip(str(o), 60), callback_data=f"ans:{pid}:{i}")]
                    for i, o in enumerate(qs[0]["options"])]
            rows.append([InlineKeyboardButton(text="Hold it", callback_data=f"hold:{pid}")])
            markup = InlineKeyboardMarkup(inline_keyboard=rows)
        msg = await self._send(text, screenshot_png, reply_markup=markup)
        mid = msg.message_id if msg is not None else None
        return [mid] * len(qs)

    async def notify(self, text: str, screenshot_png: bytes | None = None) -> None:
        if not self.enabled:
            return
        await self._send(text, screenshot_png)

    # ------------------------------------------------------------------ handlers
    def _build_dispatcher(self) -> Dispatcher:
        dp = Dispatcher()
        owner = F.from_user.id == self.owner_chat_id
        dp.message.register(self._on_remember, owner, Command("remember"))
        dp.message.register(self._on_status, owner, Command("status"))
        dp.message.register(self._on_reply, owner, F.reply_to_message, F.text)
        dp.message.register(self._on_other_owner_message, owner)
        dp.message.register(self._ignore_message)
        dp.callback_query.register(self._on_answer_button, owner, F.data.startswith("ans:"))
        dp.callback_query.register(self._on_hold_button, owner, F.data.startswith("hold:"))
        dp.callback_query.register(self._on_remember_button, owner, F.data.startswith("rem:"))
        dp.callback_query.register(self._ignore_callback)
        return dp

    async def _ignore_message(self, message: Message) -> None:
        log.debug("telegram: ignored message from chat %s", message.chat.id if message.chat else "?")

    async def _ignore_callback(self, cb: CallbackQuery) -> None:
        log.debug("telegram: ignored callback from user %s", cb.from_user.id if cb.from_user else "?")
        try:
            await cb.answer()
        except Exception:
            pass

    async def _on_other_owner_message(self, message: Message) -> None:
        await message.answer("Reply to a question message to answer it (one line per question, or 'hold').\n"
                             "/status — job counts\n/remember <question>: <answer> — store a standing answer")

    @staticmethod
    def _describe(res: dict) -> str:
        if not res.get("ok"):
            return f"Not saved: {res.get('reason', 'unknown')}"
        lines = [f"Saved: {s['key']} = {_shown(s['key'], s['answer'])}" for s in res.get("saved", [])]
        for k in res.get("declined", []):
            lines.append(f"Held: {k}")
        if res.get("unanswered"):
            lines.append("Still waiting on: " + ", ".join(res["unanswered"]))
        lines.append(f"(released {res.get('released', 0)} held job(s))")
        return "\n".join(lines)

    async def _on_reply(self, message: Message) -> None:
        text = (message.text or "").strip()
        mid = message.reply_to_message.message_id
        if text.lower() in ("hold", "hold it"):
            res = questions.BROKER.hold_it(mid)
            if res.get("ok"):
                await message.answer("Held: " + ", ".join(res["keys"]) + " — job(s) parked for you.")
            else:
                await message.answer(f"Nothing to hold: {res.get('reason')}")
            return
        res = questions.BROKER.deliver(mid, text, message.from_user.id)
        await message.answer(self._describe(res))

    async def _on_answer_button(self, cb: CallbackQuery) -> None:
        try:
            _tag, pid_s, idx_s = (cb.data or "").split(":", 2)
            pid, idx = int(pid_s), int(idx_s)
        except ValueError:
            await cb.answer("Bad button")
            return
        row = db.connect().execute("SELECT options FROM pending_questions WHERE id=?", (pid,)).fetchone()
        opts = questions._row_dict(row).get("options") if row else None
        if not opts or not (0 <= idx < len(opts)):
            await cb.answer("That question is no longer pending")
            return
        res = questions.BROKER.answer_pending(pid, str(opts[idx]), cb.from_user.id)
        await cb.answer("Saved" if res.get("ok") else str(res.get("reason")))
        await self._finish_buttons(cb, self._describe(res))

    async def _on_hold_button(self, cb: CallbackQuery) -> None:
        try:
            pid = int((cb.data or "").split(":", 1)[1])
        except (ValueError, IndexError):
            await cb.answer("Bad button")
            return
        res = questions.BROKER.hold_pending(pid)
        await cb.answer("Held" if res.get("ok") else str(res.get("reason")))
        if res.get("ok"):
            await self._finish_buttons(cb, "Held: " + ", ".join(res["keys"]) + " — job(s) parked for you.")

    async def _finish_buttons(self, cb: CallbackQuery, text: str) -> None:
        msg = cb.message
        if msg is None:
            return
        try:
            await msg.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        try:
            await msg.answer(text)
        except Exception as e:
            log.warning("telegram: follow-up failed: %s", type(e).__name__)

    async def _on_remember(self, message: Message, command: CommandObject) -> None:
        text = (command.args or "").strip()
        if not text:
            await message.answer("Usage: /remember <question>: <answer>")
            return
        if ":" in text:
            left, right = text.split(":", 1)
            key = answers.key_from_question(left)
            answer = right.strip()
            alias = left.strip()
        else:
            key = answers.key_from_question(text)
            answer = ""
            alias = text
        if not answer:
            await message.answer(f"Proposed key: {key}\nI need an answer too: /remember {alias}: <answer>")
            return
        self._prune_proposals()
        token = secrets.token_hex(4)
        self._proposals[token] = {"key": key, "answer": answer, "alias": alias,
                                  "message_id": message.message_id, "ts": time.time()}
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Confirm", callback_data=f"rem:ok:{token}"),
            InlineKeyboardButton(text="Cancel", callback_data=f"rem:no:{token}"),
        ]])
        await message.answer(f"Save <code>{html.escape(key)}</code> = <code>{html.escape(_shown(key, answer))}</code>?",
                             reply_markup=kb, parse_mode="HTML")

    async def _on_remember_button(self, cb: CallbackQuery) -> None:
        parts = (cb.data or "").split(":", 2)
        if len(parts) != 3:
            await cb.answer("Bad button")
            return
        _tag, verdict, token = parts
        prop = self._proposals.pop(token, None)
        if prop is None:
            await cb.answer("That proposal expired")
            return
        if verdict != "ok":
            await cb.answer("Cancelled")
            await self._finish_buttons(cb, "Cancelled.")
            return
        released = answers.save_answer(prop["key"], prop["answer"], answer_type="text", source="remember",
                                       source_ref=str(prop["message_id"]), aliases=[prop["alias"]])
        await cb.answer("Saved")
        await self._finish_buttons(
            cb, f"Saved: {prop['key']} = {_shown(prop['key'], prop['answer'])} (released {released} held job(s))")

    def _prune_proposals(self) -> None:
        cutoff = time.time() - PROPOSAL_TTL_S
        for t in [t for t, p in self._proposals.items() if p["ts"] < cutoff]:
            self._proposals.pop(t, None)

    async def _on_status(self, message: Message) -> None:
        con = db.connect()
        rows = con.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status ORDER BY status").fetchall()
        lines = ["Jobs:"] + ([f"  {r['status']}: {r['n']}" for r in rows] or ["  (none)"])
        lines.append(f"Pending questions: {questions.BROKER.pending_count()}")
        overdue = questions.BROKER.expire_overdue()
        if overdue:
            lines.append(f"  past deadline: {overdue}")
        await message.answer("\n".join(lines))

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        """Poll forever; restart after a pause on any error so no update is lost (Telegram keeps
        undelivered updates for 24h only)."""
        global CHANNEL
        if not self.enabled:
            log.info("telegram disabled (no token or owner chat id); questions will time out and hold")
            await asyncio.sleep(0)
            return
        CHANNEL = self
        questions.BROKER.register_sender(self.send_questions)
        self.bot = Bot(token=self.token)
        self._started = True
        try:
            while True:
                dp = self._build_dispatcher()
                try:
                    log.info("telegram: polling as owner chat %s", self.owner_chat_id)
                    await dp.start_polling(self.bot, handle_signals=False, close_bot_session=False,
                                           allowed_updates=["message", "callback_query"])
                    log.info("telegram: polling stopped")
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("telegram: polling failed (%s: %s); restarting in %ss",
                                type(e).__name__, _clip(str(e), 200), RESTART_DELAY_S)
                    await asyncio.sleep(RESTART_DELAY_S)
        finally:
            try:
                await self.bot.session.close()
            except Exception:
                pass
            self._started = False


async def notify_owner(text: str, png: bytes | None = None) -> None:
    """Module-level notification through the running channel, if any."""
    if CHANNEL is None or not CHANNEL.enabled:
        log.debug("telegram: no channel; notification dropped: %s", _clip(text, 120))
        return
    await CHANNEL.notify(text, png)
