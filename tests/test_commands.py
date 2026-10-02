import pytest

from app.core.commands import parse_command
from app.core.results import CommandError


class TestParseCommand:
    def test_command_with_args(self):
        assert parse_command("/add buy milk") == ("/add", "buy milk")

    def test_command_without_args(self):
        assert parse_command("/notes") == ("/notes", "")

    def test_strips_surrounding_whitespace(self):
        assert parse_command("  /remind 5 call mum  ") == (
            "/remind",
            "5 call mum",
        )

    def test_collapses_leading_space_in_args(self):
        assert parse_command("/add    milk") == ("/add", "milk")

    def test_preserves_internal_spacing(self):
        assert parse_command("/add buy  milk") == ("/add", "buy  milk")

    def test_rejects_missing_slash(self):
        with pytest.raises(CommandError):
            parse_command("add milk")

    def test_rejects_empty_input(self):
        with pytest.raises(CommandError):
            parse_command("   ")
