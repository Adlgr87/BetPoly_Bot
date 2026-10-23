"""Dobles mínimos de Telegram para probar los handlers sin red.

Los handlers solo tocan una docena de atributos de ``Update``/``Message``/
``CallbackQuery``, así que un objeto de verdad no aporta nada aquí: lo que
importa es **qué texto y qué teclado** salen. Cada envío queda registrado en
:class:`Recorder`, y ``recorder.buttons()`` devuelve los botones del último
teclado para comprobar la fila «🔧 Admin» (F-01/F-14).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from telegram import Chat
from telegram.error import BadRequest


class Recorder:
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def add(self, kind: str, text: Any, kwargs: Dict[str, Any], **extra) -> None:
        self.events.append({"kind": kind, "text": text, "kwargs": kwargs, **extra})

    # -- consultas cómodas -------------------------------------------------
    @property
    def last(self) -> Dict[str, Any]:
        assert self.events, "no se envió ningún mensaje"
        return self.events[-1]

    @property
    def texts(self) -> List[str]:
        return [e["text"] for e in self.events]

    @property
    def all_text(self) -> str:
        return "\n".join(str(e["text"]) for e in self.events)

    def buttons(self, index: int = -1) -> List[Any]:
        if not self.events:
            return []
        markup = self.events[index]["kwargs"].get("reply_markup")
        if markup is None:
            return []
        return [button for row in markup.inline_keyboard for button in row]

    def callbacks(self, index: int = -1) -> List[str]:
        return [b.callback_data for b in self.buttons(index) if b.callback_data]

    def reset(self) -> None:
        self.events.clear()


class FakeUser:
    def __init__(self, user_id: int, username: str = "tester", first_name: str = "Tester"):
        self.id = user_id
        self.username = username
        self.first_name = first_name
        self.full_name = first_name
        self.is_bot = False


class FakeChat:
    def __init__(self, chat_id: int = 1, chat_type: str = Chat.PRIVATE):
        self.id = chat_id
        self.type = chat_type
        self.title = "chat"


class FakeMessage:
    _next_id = 100

    def __init__(
        self,
        recorder: Recorder,
        text: Optional[str] = None,
        *,
        chat: Optional[FakeChat] = None,
        reply_to_message: Optional[FakeMessage] = None,
    ):
        self.recorder = recorder
        self.text = text
        self.chat = chat or FakeChat()
        self.reply_to_message = reply_to_message
        FakeMessage._next_id += 1
        self.message_id = FakeMessage._next_id

    async def reply_text(self, text, **kwargs):
        self.recorder.add("reply", text, kwargs, message_id=self.message_id)
        return FakeMessage(self.recorder, text=text, chat=self.chat)


class FakeCallbackQuery:
    def __init__(self, recorder: Recorder, data: str, user: FakeUser, message: FakeMessage):
        self.recorder = recorder
        self.data = data
        self.from_user = user
        self.message = message
        self.answers: List[Dict[str, Any]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append({"text": text, "alert": show_alert})

    async def edit_message_text(self, text, **kwargs):
        self.recorder.add("edit", text, kwargs, message_id=self.message.message_id)
        return self.message


class FailingCallbackQuery(FakeCallbackQuery):
    """Emula el «query is too old» de Telegram para probar ``safe_answer``."""

    async def answer(self, text=None, show_alert=False):
        raise BadRequest("Query is too old and response timeout expired")


class FakeBot:
    def __init__(self, recorder: Recorder, error: Optional[Exception] = None):
        self.recorder = recorder
        self.error = error
        self.sent: List[Dict[str, Any]] = []

    async def send_message(self, chat_id, text, **kwargs):
        if self.error:
            raise self.error
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        self.recorder.add("send", text, kwargs, chat_id=chat_id)
        return FakeMessage(self.recorder, text=text)

    async def set_my_commands(self, commands):
        self.commands = commands

    async def set_chat_menu_button(self, menu_button=None):
        self.menu_button = menu_button


class FakeContext:
    def __init__(self, recorder: Recorder, bot: Optional[FakeBot] = None):
        self.recorder = recorder
        self.user_data: Dict[str, Any] = {}
        self.chat_data: Dict[str, Any] = {}
        self.bot = bot or FakeBot(recorder)
        self.error = None
        self.application = None


class FakeUpdate:
    def __init__(
        self,
        recorder: Recorder,
        user: FakeUser,
        *,
        chat: Optional[FakeChat] = None,
        message: Optional[FakeMessage] = None,
        callback_query: Optional[FakeCallbackQuery] = None,
    ):
        self.recorder = recorder
        self.effective_user = user
        self.effective_chat = chat or (message.chat if message else FakeChat())
        self.effective_message = message
        self.callback_query = callback_query


def make_update(
    *,
    user_id: int = 1,
    username: str = "tester",
    text: Optional[str] = None,
    data: Optional[str] = None,
    chat_id: Optional[int] = None,
    chat_type: str = Chat.PRIVATE,
    reply_to: Optional[FakeMessage] = None,
    recorder: Optional[Recorder] = None,
    failing_answer: bool = False,
):
    """Construye ``(update, context, recorder)`` para un mensaje o un callback."""
    recorder = recorder or Recorder()
    user = FakeUser(user_id, username=username)
    chat = FakeChat(chat_id if chat_id is not None else user_id, chat_type)
    message = FakeMessage(recorder, text=text, chat=chat, reply_to_message=reply_to)
    query = None
    if data is not None:
        cls = FailingCallbackQuery if failing_answer else FakeCallbackQuery
        query = cls(recorder, data, user, FakeMessage(recorder, chat=chat))
    update = FakeUpdate(
        recorder, user, chat=chat, message=message, callback_query=query
    )
    return update, FakeContext(recorder), recorder
