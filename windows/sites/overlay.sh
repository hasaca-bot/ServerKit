#!/usr/bin/env bash
# Publish a site's deployment overlay (Dockerfile etc.) as the local-only
# "serverkit" branch of its mirror in /srv/git, on top of the upstream branch.
# The upstream GitHub repository is never written to.
#   overlay.sh <repo-name> <upstream-branch> <overlay-dir>
set -euo pipefail
name="$1"; upstream="$2"; overlay="$3"
bare="/srv/git/$name.git"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
git clone --quiet --branch "$upstream" "$bare" "$work/src"
cd "$work/src"
cp -a "$overlay"/. .
rm -f site.json
git add -A
git -c user.name="ServerKit deploy" -c user.email="deploy@serverkit.local" \
    commit --quiet -m "Add ServerKit deployment overlay" || echo "overlay unchanged"
git push --quiet --force origin "HEAD:refs/heads/serverkit"
echo "$name: serverkit branch -> $(git rev-parse --short HEAD)"
