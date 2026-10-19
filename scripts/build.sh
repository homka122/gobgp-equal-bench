#!/usr/bin/env bash
# Builds gobgpd for every variant under test into work/bin/<variant>/gobgpd:
#   base    upstream master
#   order   base + attribute order fix
#   alloc   base + all allocation commits
#   both    base + all fixes
# Each variant is a git worktree in work/variants/<variant>.
set -euo pipefail
cd "$(dirname "$0")/.."
source versions.env
ROOT=$PWD
WORK=$ROOT/work
SRC=$WORK/src/gobgp
GIT="git -c user.name=bench -c user.email=bench@localhost -c advice.detachedHead=false"

if [ ! -d "$SRC" ]; then
	$GIT clone --quiet --no-checkout "${GOBGP_REPO_LOCAL:-$GOBGP_REPO}" "$SRC"
fi
$GIT -C "$SRC" fetch --quiet "${GOBGP_REPO_LOCAL:-$GOBGP_REPO}" "$BASE" "$ORDER" "$ALLOC1" "$ALLOC2" "$ALLOC3" 2>/dev/null ||
	$GIT -C "$SRC" fetch --quiet "${GOBGP_REPO_LOCAL:-$GOBGP_REPO}"

variant() {
	local name=$1 start=$2
	shift 2
	local dir=$WORK/variants/$name
	if [ -d "$dir" ]; then
		$GIT -C "$SRC" worktree remove --force "$dir"
	fi
	$GIT -C "$SRC" worktree add --quiet --detach "$dir" "$start"
	if [ $# -gt 0 ]; then
		$GIT -C "$dir" cherry-pick --quiet "$@" >/dev/null
	fi
	echo "== $name: $(git -C "$dir" log --format='%h %s' -1)"
	git -C "$dir" diff --stat "$BASE" -- ':!*_test.go' | sed 's/^/   /'
	(cd "$dir" && CGO_ENABLED=0 go build -trimpath -o "$WORK/bin/$name/gobgpd" ./cmd/gobgpd)
}

variant base "$BASE"
variant order "$ORDER"
variant alloc "$BASE" "$ALLOC1" "$ALLOC2" "$ALLOC3"
variant both "$ALLOC3"

# blaster and sink use pkg/packet/bgp of the base variant
(cd live && go build -trimpath -o "$WORK/bin/blaster" ./cmd/blaster && go build -trimpath -o "$WORK/bin/sink" ./cmd/sink)
echo "built: $(ls "$WORK/bin" | tr '\n' ' ')"
