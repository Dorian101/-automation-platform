"""The bot's button menus and the step-by-step input flow.

Telegram keyboards send only fixed text; they cannot ask for an argument. The
commands that need input are therefore reached through a short state machine:
tap the command, then type the argument. The next field's hint is shown in
``input_field_placeholder`` — a grey prompt in the input box — rather than as a
message, so nothing floods the chat while the reader is deciding what to type.

This is transport code and belongs here, not in a plugin: the plugins keep
their transport-free ``execute``, and this module only gathers arguments and
hands them over.
"""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

# Group buttons, fixed text. A button is just a message the bot answers; the
# group names below are what that message contains.
BTN_ARGS = "➕ Команды с аргументами"
BTN_SHOW = "📋 Вывод данных"
BTN_HELP = "❓ Помощь"
BTN_BACK = "← Назад"

BTN_NOTE = "📝 Заметка"
BTN_REMIND = "⏰ Напоминание"
BTN_NOTES = "📚 Заметки"
BTN_REMINDS = "⏳ Напоминания"


class InputFlow(StatesGroup):
    waiting: State = State()
    note: State = State()
    remind_minutes: State = State()
    remind_text: State = State()


def _keyboard(
    rows: list[list[str]], placeholder: str | None = None
) -> ReplyKeyboardMarkup:
    """A permanent keyboard from strings, with a hint in the input box."""
    buttons = [[KeyboardButton(text=label) for label in row] for row in rows]

    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        input_field_placeholder=placeholder,
    )


def main_keyboard() -> ReplyKeyboardMarkup:
    return _keyboard([[BTN_ARGS, BTN_SHOW], [BTN_HELP]])


def args_keyboard() -> ReplyKeyboardMarkup:
    return _keyboard(
        [[BTN_NOTE, BTN_REMIND], [BTN_BACK]],
        placeholder="Выберите команду",
    )


def show_keyboard() -> ReplyKeyboardMarkup:
    return _keyboard(
        [[BTN_NOTES, BTN_REMINDS], [BTN_BACK]],
        placeholder="Что показать?",
    )


def build_menu_router(execute) -> Router:
    """The menu and input flow, wired to the manager's execute.

    ``execute`` is ``PluginManager.execute`` — the same path a typed command
    takes, so a button and the text it replaces cannot disagree about what a
    command does.
    """
    router = Router(name="menu")

    @router.message(F.text == "/start")
    async def start(message: Message) -> None:
        await message.answer(
            "Платформа автоматизации.\n"
            "Выберите действие кнопкой ниже или введите команду.",
            reply_markup=main_keyboard(),
        )

    # -- grouping, no state ------------------------------------------------

    @router.message(F.text == BTN_SHOW)
    async def open_show(message: Message) -> None:
        await message.answer("Что показать?", reply_markup=show_keyboard())

    @router.message(F.text == BTN_NOTES)
    async def show_notes(message: Message) -> None:
        result = await execute("/notes", "", _identity(message))
        await message.answer(result.text, reply_markup=show_keyboard())

    @router.message(F.text == BTN_REMINDS)
    async def show_reminders(message: Message) -> None:
        result = await execute("/reminders", "", _identity(message))
        await message.answer(result.text, reply_markup=show_keyboard())

    @router.message(F.text == BTN_HELP)
    async def show_help(message: Message) -> None:
        result = await execute("/help", "", _identity(message))
        await message.answer(result.text, reply_markup=main_keyboard())

    @router.message(F.text == BTN_BACK)
    async def back(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Выберите действие.", reply_markup=main_keyboard())

    # -- the input flow, stateful ------------------------------------------

    @router.message(F.text == BTN_ARGS)
    async def open_args(message: Message, state: FSMContext) -> None:
        await state.set_state(InputFlow.waiting)
        await message.answer(
            "Выберите, что создать.", reply_markup=args_keyboard()
        )

    @router.message(InputFlow.waiting, F.text == BTN_NOTE)
    async def ask_note(message: Message, state: FSMContext) -> None:
        await state.set_state(InputFlow.note)
        await message.answer(
            "Что записать?",
            reply_markup=_keyboard([[BTN_BACK]], placeholder="Что записать?"),
        )

    @router.message(InputFlow.waiting, F.text == BTN_REMIND)
    async def ask_remind_minutes(message: Message, state: FSMContext) -> None:
        await state.set_state(InputFlow.remind_minutes)
        await message.answer(
            "Через сколько минут?",
            reply_markup=_keyboard(
                [[BTN_BACK]], placeholder="Через сколько минут?"
            ),
        )

    @router.message(InputFlow.note)
    async def save_note(message: Message, state: FSMContext) -> None:
        if not message.text:
            return

        try:
            result = await execute("/add", message.text, _identity(message))
        except Exception as error:
            await message.answer(str(error), reply_markup=args_keyboard())
            await state.set_state(InputFlow.waiting)
            return

        await state.set_state(InputFlow.waiting)
        await message.answer(result.text, reply_markup=args_keyboard())

    @router.message(InputFlow.remind_minutes)
    async def ask_remind_text(message: Message, state: FSMContext) -> None:
        if not message.text:
            return

        await state.update_data(minutes=message.text)
        await state.set_state(InputFlow.remind_text)
        await message.answer(
            "Текст напоминания",
            reply_markup=_keyboard(
                [[BTN_BACK]], placeholder="Текст напоминания"
            ),
        )

    @router.message(InputFlow.remind_text)
    async def save_reminder(message: Message, state: FSMContext) -> None:
        if not message.text:
            return

        minutes = (await state.get_data()).get("minutes", "")
        args = f"{minutes} {message.text}".strip()

        try:
            result = await execute("/remind", args, _identity(message))
        except Exception as error:
            await message.answer(str(error), reply_markup=args_keyboard())
            await state.set_state(InputFlow.waiting)
            return

        await state.set_state(InputFlow.waiting)
        await message.answer(result.text, reply_markup=args_keyboard())

    return router


def _identity(message: Message):
    from app.core.identity import telegram

    return telegram(message.chat.id)
