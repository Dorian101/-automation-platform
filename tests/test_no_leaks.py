"""Nothing in this repository names a host, an address or a domain.

This is a project that goes on the open internet, and its documentation is
the part a stranger reads first. A runbook that ships one deployment's
address is not a template: it tells the next reader to point their Caddy at
somebody else's server, and it publishes an address and two domains to anyone
who opens the repository.

**What was left out, and why.** The obvious rule — "no dotted things" — cannot
be used. `os.getenv` and `app.core.results` are shaped exactly like a domain,
and a check that fires on a Python import is a check that gets switched off.
So this looks for the two shapes a leak actually takes:

- an **IPv4 literal** that is not loopback, not a reserved range the
  documentation names on purpose, and not an address the RFC reserves for
  examples;
- a **host inside a URL**, which is where a domain shows up: a `curl` in the
  runbook, a Caddy site block, a default in the code.

A bare name in prose is not checked, and that is a deliberate limit rather
than an oversight. It is worth saying out loud: this catches an address and a
URL, it does not catch somebody writing "ask the vendor" instead of a name.

Checked over tracked files, so it reads the repository rather than whatever
happens to be in the working directory.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Documentation, configuration, templates and code — everything a reader or a
#: deployer can see.
SCANNED_SUFFIXES = {".md", ".py", ".html", ".yml", ".yaml", ".sql"}

#: A dotted run of four octets. Ranges are not hosts, and the documentation
#: names three of them on purpose.
IP_LIKE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

#: The host of a URL. The character class stops at anything that cannot be in
#: a host, which is what keeps a trailing comma, a `<placeholder>` and a
#: format string out of the result.
URL_HOST = re.compile(r"https?://([A-Za-z0-9][A-Za-z0-9.-]*)")

#: Addresses nobody owns. 127.0.0.0/8 is this machine. 198.18.0.0/15 is the
#: benchmark range the DNS section explains a symptom with. 192.0.2.0/24,
#: 198.51.100.0/24 and 203.0.113.0/24 are what RFC 5737 reserves for exactly
#: this.
ALLOWED_IPS = {
    "127.0.0.1",
    "1.1.1.1",
    "8.8.8.8",
    "198.18.0.0",
    "192.0.2.0",
    "198.51.100.0",
    "203.0.113.0",
}

#: Hosts that belong to a service rather than to a person. Anything not here
#: has to be an example name, and `example.com` is reserved for that.
ALLOWED_HOSTS = {
    "example.com",
    "public.example",
    "localhost",
    "telegram.org",
    "oauth.telegram.org",
    "api.telegram.org",
    "github.com",
    "dns.google",
    "cloudflare-dns.com",
    "astral.sh",
    "sitemaps.org",
    "www.sitemaps.org",
    # Named in examples and in the reader's own public page, and belonging to
    # nobody who deploys this: a vacancy URL in the applications plugin, a
    # bot handle, and a link on the about page.
    "hh.ru",
    "x.ru",
    "t.me",
    "it.cscentr.com",
    "1.1.1.1",
    "8.8.8.8",
}


def tracked_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    return [
        ROOT / name
        for name in listed.stdout.split()
        if Path(name).suffix in SCANNED_SUFFIXES
    ]


def read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


@pytest.mark.parametrize(
    "path", tracked_files(), ids=lambda p: str(p.relative_to(ROOT))
)
class TestNothingIsNamed:
    def test_no_address(self, path: Path):
        text = read(path)

        if text is None:
            return

        found = {
            value
            for value in IP_LIKE.findall(text)
            if value not in ALLOWED_IPS
            and not value.startswith("127.0.0")
            # The documentation names these ranges as ranges; anything else in
            # them is not an address either.
            and not value.startswith(("198.18.", "192.0.2.", "198.51.100.",
                                      "203.0.113."))
        }

        assert found == set(), f"{path.name}: {sorted(found)}"

    def test_no_host_in_a_url(self, path: Path):
        text = read(path)

        if text is None:
            return

        found = {
            host
            for host in URL_HOST.findall(text)
            if host not in ALLOWED_HOSTS
            and not host.startswith(("127.0.0", "198.18."))
            and not re.match(r"^\d{1,3}(\.\d{1,3})*$", host)
        }

        assert found == set(), f"{path.name}: {sorted(found)}"
