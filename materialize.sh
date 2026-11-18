#!/bin/sh
set -eu
src=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
dest=${1:?usage: materialize.sh NEW_DIRECTORY}
test ! -e "$dest"
url=$(sed -n '1p' "$src/UPSTREAM"); rev=$(sed -n '2p' "$src/UPSTREAM")
mkdir -- "$dest"; git -C "$dest" init -q
git -C "$dest" fetch -q --depth 1 "$url" "$rev"
test "$(git -C "$dest" rev-parse FETCH_HEAD)" = "$rev"
git -C "$dest" checkout -q --detach "$rev"; cp -R "$src/overlay/." "$dest/"
git -C "$dest" apply --unidiff-zero "$src/patches/optional-tool-config.patch"
