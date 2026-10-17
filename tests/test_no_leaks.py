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
happens to be in the working directory — and in two tests rather than one per
file per rule, because 216 tests that each read a file to find nothing is a
slower way of saying nothing was found, and it makes the real failure harder
to see in the output.
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
    # The XML namespace, which is an identifier rather than a place to fetch:
    # an SVG document is not valid without it and no browser ever resolves it.
    # The one that matters is the favicon, which is served as a standalone
    # document and therefore needs the declaration that an inline one does not.
    "www.w3.org",
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


@pytest.fixture(scope="module")
def tracked() -> list[tuple[str, str]]:
    """Every tracked documentation, configuration and source file, with its text.

    Read once. The first version of this ran a test per file per rule, which
    meant reading every file twice to find nothing.
    """
    listed = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    files = []

    for name in listed.stdout.split():
        path = ROOT / name

        if path.suffix not in SCANNED_SUFFIXES:
            continue

        try:
            files.append((name, path.read_text(encoding="utf-8")))
        except (UnicodeDecodeError, OSError):
            continue

    return files


def test_no_address(tracked):
    found = {
        f"{name}: {value}"
        for name, text in tracked
        for value in IP_LIKE.findall(text)
        if not _allowed_ip(value)
    }

    assert found == set(), "\n".join(sorted(found))


def test_no_host_in_a_url(tracked):
    found = {
        f"{name}: {host}"
        for name, text in tracked
        for host in URL_HOST.findall(text)
        if not _allowed_host(host)
    }

    assert found == set(), "\n".join(sorted(found))


def _allowed_ip(value: str) -> bool:
    return (
        value in ALLOWED_IPS
        or value.startswith("127.0.0")
        or value.startswith("198.18.")
        or value.startswith("192.0.2.")
        or value.startswith("198.51.100.")
        or value.startswith("203.0.113.")
    )


def _allowed_host(host: str) -> bool:
    # A bare address in a URL is the previous test's business, not this one's.
    return (
        host in ALLOWED_HOSTS
        or host.startswith(("127.0.0", "198.18."))
        or bool(re.fullmatch(r"\d{1,3}(\.\d{1,3})*", host))
    )
