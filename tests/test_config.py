import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

READ_CONFIG = (
    "import app.core.config as c; print(repr(c.Config.DB_PASSWORD))"
)


def _read_config_with_env(cwd: Path) -> str:
    result = subprocess.run(
        [sys.executable, "-c", READ_CONFIG],
        cwd=cwd,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(REPO_ROOT),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


class TestDotenvLoading:
    def test_config_reads_dotenv_file(self, tmp_path):
        """Config must not depend on the entry point importing in the right order.

        main.py calls load_dotenv() after the app imports, which is too late for
        class attributes evaluated at import time. Regression test: importing
        app.core.config first must still see the value from .env.
        """
        (tmp_path / ".env").write_text("DB_PASSWORD=secret-from-dotenv\n")

        assert _read_config_with_env(tmp_path) == "'secret-from-dotenv'"

    def test_environment_takes_precedence_over_dotenv(self, tmp_path):
        (tmp_path / ".env").write_text("DB_PASSWORD=from-dotenv\n")

        result = subprocess.run(
            [sys.executable, "-c", READ_CONFIG],
            cwd=tmp_path,
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": str(REPO_ROOT),
                "DB_PASSWORD": "from-environment",
            },
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "'from-environment'"
