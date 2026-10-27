"""The button menus: what the keyboard shows, and that the flow is wired.

The state machine itself is exercised here through the keyboard builders — the
full click-through needs a running dispatcher and a fed update, which the
integration tests cover elsewhere. What is worth a unit test is that the
keyboards carry the right buttons and that the argument flow's prompts are the
field's placeholder, not a chat message.
"""

from app.bot import menu
from app.bot.menu import (
    BTN_ARGS,
    BTN_BACK,
    BTN_HELP,
    BTN_NOTE,
    BTN_NOTES,
    BTN_REMIND,
    BTN_REMINDS,
    BTN_SHOW,
    InputFlow,
)


def _labels(keyboard):
    return [button.text for row in keyboard.keyboard for button in row]


class TestKeyboardGroups:
    def test_the_main_menu_offers_three_groups(self):
        labels = _labels(menu.main_keyboard())

        assert labels == [BTN_ARGS, BTN_SHOW, BTN_HELP]

    def test_the_args_menu_offers_two_creators_and_back(self):
        labels = _labels(menu.args_keyboard())

        assert labels == [BTN_NOTE, BTN_REMIND, BTN_BACK]

    def test_the_show_menu_offers_two_reads_and_back(self):
        labels = _labels(menu.show_keyboard())

        assert labels == [BTN_NOTES, BTN_REMINDS, BTN_BACK]


class TestPrompts:
    def test_the_create_prompt_is_a_placeholder_not_a_message(self):
        assert menu.args_keyboard().input_field_placeholder == "Выберите команду"

    def test_the_show_prompt_is_a_placeholder(self):
        assert menu.show_keyboard().input_field_placeholder == "Что показать?"


class TestInputFlow:
    def test_the_flow_has_one_state_per_step(self):
        states = (
            InputFlow.waiting,
            InputFlow.note,
            InputFlow.remind_minutes,
            InputFlow.remind_text,
        )

        assert len(states) == 4

    def test_building_the_router_needs_no_transport(self):
        async def noop(command, args, identity):
            from app.core.results import reply

            return reply("ok")

        router = menu.build_menu_router(noop)

        assert router.name == "menu"
