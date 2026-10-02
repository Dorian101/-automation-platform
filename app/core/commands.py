from app.core.results import CommandError


def parse_command(text: str) -> tuple[str, str]:
    """Split user input into a command and its arguments.

    ``"/add buy milk"`` becomes ``("/add", "buy milk")``.
    ``"/notes"`` becomes ``("/notes", "")``.
    """
    stripped = text.strip()

    if not stripped.startswith("/"):
        raise CommandError("Command must start with /")

    head, _, rest = stripped.partition(" ")

    return head, rest.strip()
