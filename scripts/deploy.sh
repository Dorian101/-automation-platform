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
git pull --ff-only

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
journalctl -u automation-platform -n 50 --no-pager
