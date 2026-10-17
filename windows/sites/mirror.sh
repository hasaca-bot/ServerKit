#!/usr/bin/env bash
# Create or refresh the local mirror /srv/git/<name>.git from a working copy.
#
# The panel builds sites from these mirrors (served read-only on loopback by
# serverkit-git-local). The source is a git working copy on this PC — the
# project the owner is working on — so what gets published is exactly its
# committed branches, without going through GitHub. Uncommitted changes are
# not published; the caller is told so.
#
# Usage (as root inside the distro):
#   mirror.sh <name> <path-to-working-copy-in-wsl> [<branch>]
# Prints the branch that was published.

set -euo pipefail

name="${1:?name required}"; src="${2:?source path required}"; branch="${3:-}"
case "$name" in *[!a-z0-9-]*) echo "invalid name: $name" >&2; exit 2 ;; esac
[ -d "$src/.git" ] || [ -f "$src/HEAD" ] || { echo "not a git repository: $src" >&2; exit 1; }

export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*'

if [ -z "$branch" ]; then
    branch="$(git -C "$src" rev-parse --abbrev-ref HEAD)"
    [ "$branch" != "HEAD" ] || { echo "the working copy is on a detached HEAD; pass a branch" >&2; exit 1; }
fi
if [ -n "$(git -C "$src" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
    echo "note: $src has uncommitted changes; only committed work is published" >&2
fi

bare="/srv/git/$name.git"
if [ ! -d "$bare" ]; then
    git init --quiet --bare "$bare"
fi
# Force-update every branch from the working copy; the serverkit overlay
# branch lives only in the mirror and is rebuilt by overlay.sh.
git --git-dir="$bare" fetch --quiet --force "$src" "+refs/heads/*:refs/heads/*"
git --git-dir="$bare" symbolic-ref HEAD "refs/heads/$branch"
echo "$name: mirror updated, branch $branch at $(git --git-dir="$bare" rev-parse --short "refs/heads/$branch")"
