#!/usr/bin/env bash
# Get a Metabase source checkout at the version this driver targets (metabase.version), with this
# repository linked in as modules/drivers/hardcharger and registered in Metabase's deps files.
#
#   ci/checkout-metabase.sh [DIR]     prints DIR on its last line
#
# DIR defaults to $METABASE_DIR, then ~/.cache/metabase-hardcharger-driver/metabase-<version>. It must
# be outside this repository: the checkout links back to it. An existing checkout of another version
# is refused, never reused.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
version="$(tr -d '[:space:]' < "$root/metabase.version")"
if [ -z "$version" ]; then
  echo "metabase.version is empty" >&2
  exit 1
fi
dir="${1:-${METABASE_DIR:-$HOME/.cache/metabase-hardcharger-driver/metabase-$version}}"
case "$(mkdir -p "$dir" && cd "$dir" && pwd -P)/" in
  "$(cd "$root" && pwd -P)"/*)
    echo "the Metabase checkout ($dir) must be outside this repository" >&2
    exit 1
    ;;
esac

if [ ! -d "$dir/.git" ]; then
  # Metabase's tags are public; the clone needs no credential.
  git clone --quiet --depth 1 --branch "$version" https://github.com/metabase/metabase.git "$dir" >&2
fi
# A commit may carry several tags (v0.65.4.3 and v0.65.4.x): look for ours by name.
tags="$(git -C "$dir" tag --points-at HEAD)"
if ! grep -qxF -- "$version" <<<"$tags"; then
  echo "$dir is not at $version (tags on its commit: ${tags:-none}): remove it or name another directory" >&2
  exit 1
fi

link="$dir/modules/drivers/hardcharger"
if [ -L "$link" ]; then
  rm "$link"
elif [ -e "$link" ]; then
  echo "$link exists and is not a link: refusing to replace it" >&2
  exit 1
fi
ln -s "$root" "$link"
(cd "$dir" && python3 modules/drivers/hardcharger/ci/patch-metabase.py >&2)
echo "$dir"
