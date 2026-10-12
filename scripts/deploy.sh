#!/usr/bin/env bash
#
# Deploy: pull what was pushed, install what changed, restart, show the logs.
#
# This is what an SSH key pushed to this server is allowed to run — see
# docs/DEPLOYMENT.md, which has the authorized_keys line that says so. The
# script lives in the repository rather than being installed by hand, so it is
# versioned and changes with the same commit as everything else. The
# consequence is stated plainly: whoever can push to main can change this file,
# so the restriction is on what the key may do *besides* deploying, not on
# who can deploy. Pushing to this repository already means running code on this
# server.
#
# Run as `automation`, which owns the checkout, so the files a pull creates are
# already owned by the right user and no `chown` is needed afterwards.

set -euo pipefail

REPO=/opt/automation-platform

cd "$REPO"

# --ff-only on purpose. A plain `git pull` merges when it cannot fast-forward,
# which turns a deploy into a conflict resolution and leaves the server on a
# commit nobody pushed. Failing here says the branch moved and stops.
#
# Then hand over to the file as it is on disk *now*. This pull can rewrite the
# very file being executed — it lives in the repository it is updating — and
# bash reads a script by offset from an open handle. When the bytes under it
# change size, what runs next is whatever the new content lines up with. It
# happened here: a deploy restarted the service and then ran the *previous*
# version's log command, which had no sudo on it and so printed nothing.
#
# The failure is quiet by construction. The service was up and the deploy had
# pulled, so every signal a human watches said it worked.
#
# `exec` re-opens the file, so the rest runs the version that was just pulled
# — which is the one whose code is about to be deployed. The argument is what
# stops the loop: the second pass does not pull, so it cannot reach this again.
#
# Spelled as a path rather than `$0`: exec looks a bare name up in $PATH, not
# in the current directory, so `$0` only works when the caller happened to pass
# an absolute one. SSH does. Nothing forces it to keep doing so.
if [ "${1:-}" != "--pulled" ]; then
    git pull --ff-only
    exec "$REPO/scripts/deploy.sh" --pulled
fi

# Installed from the lockfile, never resolved: a deploy must not be the moment
# a new version of a package is chosen. Omitting this is safe right up to the
# day pyproject.toml changes, which is exactly the day it is missed.
uv sync --frozen --no-dev

# The one thing `automation` cannot do itself, granted by a single sudoers
# rule. Restarting is the only privileged step the deploy needs.
sudo systemctl restart automation-platform

# The tail is here so that whoever triggered this sees what happened without
# having to ask the host for it. A deploy that starts a service which then
# crashes leaves the answer in the journal, and this is where it gets read.
#
# The second sudo rule. It is spelled out in full, with every argument, and has
# no wildcard: this reads one unit's last 50 lines and nothing else on the host.
# The alternative — adding `automation` to systemd-journal — is one command
# instead of a rule, and grants read access to every log on the machine, for
# ever, including the ones written after this deploy is switched off.
#
# The exact arguments are also a tripwire. Change `-n 50` here and sudo rejects
# the call, which is a louder failure than it looks.
sudo journalctl -u automation-platform -n 50 --no-pager
