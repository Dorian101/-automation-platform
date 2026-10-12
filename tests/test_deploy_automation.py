"""The deploy path, checked without touching a server.

A malformed workflow is the quietest failure in the repository: the page
renders, every test passes, the push goes out, and nothing happens. These
tests parse the two files that make the deploy and assert the things that
would otherwise only be found by pushing and waiting.
"""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"
SCRIPT = ROOT / "scripts" / "deploy.sh"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def commands(script: str) -> list[str]:
    """The executable lines, with the comments left out.

    The script explains each step in a comment above it, and a comment about
    `--ff-only` contains the string `--ff-only`. A search over the whole file
    therefore finds the explanation whether or not the command is still there
    — which is how this file asserted a merge-prone pull was safe for one
    revision of its own existence.
    """
    return [
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    ]


def _triggers(workflow: dict) -> dict:
    # PyYAML reads the bare key `on` as the boolean True, because YAML 1.1
    # calls it so. Both spellings have to be handled or the test quietly
    # asserts on an empty dict.
    return workflow.get("on", workflow.get(True, {}))


class TestWorkflow:
    def test_it_parses(self, workflow):
        """If this fails, nothing else here means anything."""
        assert isinstance(workflow, dict)

    def test_it_runs_on_a_push_to_main(self, workflow):
        triggers = _triggers(workflow)

        assert triggers["push"]["branches"] == ["main"]

    def test_it_can_also_be_run_by_hand(self, workflow):
        """A deploy that only fires on push cannot be repeated without a
        commit to push, and half of needing to repeat one is a bad restart."""
        assert "workflow_dispatch" in _triggers(workflow)

    def test_deploys_do_not_overlap(self, workflow):
        """Two pushes in a row would otherwise race on the restart."""
        assert workflow["concurrency"]["cancel-in-progress"] is False

    def test_it_uses_no_third_party_action(self, workflow):
        """An action from another account has write access to this pipeline.

        The usual way to set up SSH is `webfactory/ssh-agent`, which is four
        lines of YAML replaced by somebody else's code running with this
        repository's secrets.
        """
        for step in workflow["jobs"]["deploy"]["steps"]:
            assert "uses" not in step, step

    def test_it_never_checks_the_source_out(self, workflow):
        """The host pulls. A checkout would put the code on a runner for no
        reason, and this pipeline does not need it."""
        steps = workflow["jobs"]["deploy"]["steps"]

        assert not any(
            step.get("uses", "").startswith("actions/checkout")
            for step in steps
        )

    def test_it_pins_the_host_key(self, workflow):
        """ssh-keyscan at deploy time is trust on first use, decided by
        whichever machine answers that day."""
        step = workflow["jobs"]["deploy"]["steps"][0]

        assert "DEPLOY_KNOWN_HOSTS" in step["env"]
        assert "ssh-keyscan" not in step["run"]

    def test_it_fails_fast_rather_than_waiting_for_a_password(
        self, workflow
    ):
        run = workflow["jobs"]["deploy"]["steps"][0]["run"]

        assert "BatchMode=yes" in run
        assert "set -euo pipefail" in run


class TestScript:
    @pytest.fixture
    def joined(self, script) -> str:
        return "\n".join(commands(script))

    def test_it_stops_at_the_first_failure(self, joined):
        """A half-applied deploy — pulled but not restarted — is worse than a
        failed one, because it looks like it worked."""
        assert "set -euo pipefail" in joined

    def test_it_pulls_without_merging(self, joined):
        """A plain pull merges on conflict, leaving the server on a commit
        nobody pushed."""
        assert "--ff-only" in joined

    def test_it_installs_from_the_lockfile(self, joined):
        """Resolving versions during a deploy means the deploy is also the
        moment a new package version gets chosen."""
        assert "uv sync --frozen --no-dev" in joined

    def test_it_restarts_the_service(self, joined):
        assert "systemctl restart automation-platform" in joined

    def test_it_shows_the_logs(self, joined):
        """Whoever triggered it should see what happened without asking the
        host for it.

        Through sudo, and the whole command in full. `automation` cannot read
        the journal as itself — a plain `journalctl` prints a permissions
        hint and exits non-zero, which under `set -e` fails the deploy after
        it has already restarted the service.
        """
        assert "sudo journalctl -u automation-platform -n 50 --no-pager" in joined

    def test_the_sudoers_rules_match_what_the_script_runs(self, script):
        """Every rule in the deployment docs must be a command the script
        actually runs, spelled the same way.

        sudoers matches arguments exactly, so a script that drifted from its
        rule stops working at the moment the two disagree — and only in
        production. Change `-n 50` in one file and the other has to change
        with it, which is what this catches.
        """
        runbook = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")

        rules = re.findall(
            r"^automation ALL=\(root\) NOPASSWD: (.+)$", runbook, re.MULTILINE
        )

        assert rules, "no sudoers rules found in the deployment runbook"

        for rule in rules:
            # sudoers names an absolute path, the script calls the command by
            # name through sudo.
            command = rule.removeprefix("/usr/bin/")
            assert any(
                line.endswith(command) for line in commands(script)
            ), rule

    def test_it_does_not_chown(self, script):
        """It runs as the user that owns the checkout, so the files a pull
        creates are already owned correctly.

        Read off the commands rather than the text: the script explains in a
        comment that `chown` is unnecessary, and searching for the word finds
        its own explanation.
        """
        assert not any("chown" in line for line in commands(script))


class TestSecretsStayOut:
    def test_the_environment_is_ignored(self):
        """The file the deploy reads for credentials must never be tracked."""
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

        assert ".env" in ignored

    def test_no_private_key_is_in_the_repository(self):
        """The deploy key is a GitHub secret. A PEM block in here would be a
        key that has already leaked, however carefully it is later deleted."""
        marker = "PRIVATE" + " KEY"

        for path in ROOT.rglob("*"):
            if ".git" in path.parts or ".venv" in path.parts:
                continue
            if path.suffix in {".sql", ".pyc"}:
                continue
            # This file: it has to spell out what it is looking for, so it
            # would find itself on every run.
            if path == Path(__file__).resolve():
                continue

            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue

            assert marker not in text, path
