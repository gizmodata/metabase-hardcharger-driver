#!/usr/bin/env bash
# What a release needs before anything is built: the tag is the project's version, and CHANGELOG.md
# has that version's section, which becomes the release notes.
#
#   ci/release-check.sh [TAG] [NOTES_FILE]
#
# The project's version is info.version in resources/metabase/hardcharger/metabase-plugin.yaml.
# With a TAG (vX.Y.Z) it must be "v" + that version; without one (a dry run) only the CHANGELOG
# section is checked. The section's text is written to NOTES_FILE (default release-notes.md).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tag="${1:-}"
notes="${2:-release-notes.md}"
manifest="$root/resources/metabase/hardcharger/metabase-plugin.yaml"
version="$(sed -n 's/^  version: *//p' "$manifest" | head -1)"

if ! [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "info.version in $manifest is '$version', not X.Y.Z" >&2
  exit 1
fi
if [ -n "$tag" ] && [ "$tag" != "v$version" ]; then
  echo "tag $tag is not the project's version (v$version, from metabase-plugin.yaml)" >&2
  exit 1
fi

# "## [X.Y.Z] - YYYY-MM-DD" up to the next "## [" heading.
if ! grep -qE "^## \[${version//./\\.}\] - [0-9]{4}-[0-9]{2}-[0-9]{2}$" "$root/CHANGELOG.md"; then
  echo "CHANGELOG.md has no '## [$version] - YYYY-MM-DD' section" >&2
  exit 1
fi
awk -v header="## [$version] " '
  index($0, header) == 1 { found = 1; next }
  found && /^## \[/ { exit }
  found { print }
' "$root/CHANGELOG.md" > "$notes"
if ! grep -q '[^[:space:]]' "$notes"; then
  echo "the CHANGELOG.md section for $version is empty" >&2
  exit 1
fi
echo "version $version${tag:+, tag $tag}: release notes in $notes ($(wc -l < "$notes" | tr -d ' ') lines)"
