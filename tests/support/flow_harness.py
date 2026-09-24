"""A real ``Application`` wired with the guided-flow coordinator and fake boundaries.

Groups: 0 the coordinator; 1 ``/cancel`` outside a flow, a stand-in router for
registered non-flow actions and ``/help``/``/menu``; 3 the catch-all callback
handler and the free-text fallback. Every update goes through
``Application.process_update``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from telegram.ext import CallbackQueryHandler, CommandHandler, filters

from price_tracker.bot.callbacks import Action, decode
from price_tracker.bot.flows import (
    FlowConfig,
    GuidedFlow,
    is_registered_non_flow,
    register_guided_flow,
)
from tests.support.fake_telegram import (
    Call,
    FakeRequest,
    FakeServices,
    ManualTimer,
    callback_update,
    make_application,
    message_update,
)

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import Application, CallbackContext

HELP_TEXT = "Help screen."


class Harness:
    """Builds, drives and restarts one application."""

    def __init__(
        self,
        services: FakeServices | None = None,
        *,
        config: FlowConfig | None = None,
        request: FakeRequest | None = None,
        legacy_handlers_present: bool = False,
        concurrent_updates: int = 1,
    ) -> None:
        self.concurrent_updates = concurrent_updates
        self.request = request or FakeRequest()
        self.services = services or FakeServices(active={10, 11})
        self.config = config
        self.routed: list[Action] = []
        self.help_calls = 0
        self.legacy_handlers_present = legacy_handlers_present
        self._build()

    def _build(self) -> None:
        self.app: Application[Any, Any, Any, Any, Any, Any] = make_application(
            self.request, concurrent_updates=self.concurrent_updates
        )
        self.timer = ManualTimer()
        self.flow = GuidedFlow(self.services, self.timer, config=self.config)
        register_guided_flow(
            self.app, self.flow, legacy_handlers_present=self.legacy_handlers_present
        )
        self.app.add_handler(
            CallbackQueryHandler(self._route, pattern=is_registered_non_flow), group=1
        )
        self.app.add_handler(
            CommandHandler(["help", "menu"], self._help, filters=filters.UpdateType.MESSAGE),
            group=1,
        )

    async def _route(self, update: Update, context: CallbackContext[Any, Any, Any, Any]) -> None:
        del context
        query = update.callback_query
        assert query is not None
        decoded = decode(query.data)
        assert isinstance(decoded, Action)
        self.routed.append(decoded)
        await query.answer()

    async def _help(self, update: Update, context: CallbackContext[Any, Any, Any, Any]) -> None:
        del context
        self.help_calls += 1
        assert update.message is not None
        await update.message.reply_text(HELP_TEXT)

    async def start(self) -> None:
        await self.app.initialize()

    async def stop(self) -> None:
        await self.app.shutdown()

    async def restart(self) -> None:
        """A new process: fresh application, registry and timers; same services and chat."""
        await self.stop()
        self._build()
        await self.start()

    async def process(self, update: Update) -> None:
        await self.app.update_processor.process_update(update, self.app.process_update(update))

    async def text(
        self,
        chat_id: int,
        user_id: int,
        text: str,
        *,
        kind: str = "message",
        chat_type: str | None = None,
    ) -> None:
        await self.process(
            message_update(self.app.bot, chat_id, user_id, text, kind=kind, chat_type=chat_type)
        )

    async def press(self, chat_id: int, user_id: int, data: str, *, message_id: int = 1) -> None:
        await self.process(
            callback_update(self.app.bot, chat_id, user_id, data, message_id=message_id)
        )

    def sent(self, chat_id: int) -> list[Call]:
        return [c for c in self.request.calls_of("sendMessage") if c.chat_id == chat_id]

    def texts_to(self, chat_id: int) -> list[str]:
        return [str(c.params["text"]) for c in self.sent(chat_id)]

    def last_prompt(self, chat_id: int) -> Call:
        prompts = [c for c in self.sent(chat_id) if c.callback_data()]
        assert prompts, f"no prompt was sent to chat {chat_id}"
        return prompts[-1]

    @staticmethod
    def prompt_message_id(call: Call) -> int:
        assert call.message_id is not None
        return call.message_id

    def edits_of(self, message_id: int) -> list[str]:
        return [
            str(c.params["text"])
            for c in self.request.calls_of("editMessageText")
            if int(c.params["message_id"]) == message_id
        ]

    def toasts(self) -> list[str | None]:
        return [c.params.get("text") for c in self.request.calls_of("answerCallbackQuery")]


class ServiceBarrier:
    """Suspend one service invocation; subsequent invocations remain usable."""

    def __init__(self, services: Any, phase: str, *, after: bool = False) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.original = getattr(services, phase)
        self.used = False
        self.after = after
        setattr(services, phase, self.call)

    async def call(self, *args: Any, **kwargs: Any) -> Any:
        if self.used:
            return await self.original(*args, **kwargs)
        self.used = True
        result = await self.original(*args, **kwargs) if self.after else None
        self.entered.set()
        await self.release.wait()
        return result if self.after else await self.original(*args, **kwargs)

    async def wait(self) -> None:
        await asyncio.wait_for(self.entered.wait(), 2)
