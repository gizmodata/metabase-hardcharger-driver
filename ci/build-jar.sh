#!/usr/bin/env bash
# Build the plugin jar with Metabase's own driver build, and check what is in it.
#
#   ci/build-jar.sh [METABASE_DIR]     writes dist/hardcharger.metabase-driver.jar and its .sha256
#
# Needs JDK 21 and the Clojure CLI. METABASE_DIR is a checkout made by ci/checkout-metabase.sh
# (made now if it isn't named).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
metabase="${1:-$("$root/ci/checkout-metabase.sh")}"
jar_name="hardcharger.metabase-driver.jar"
manifest="$root/resources/metabase/hardcharger/metabase-plugin.yaml"
version="$(sed -n 's/^  version: *//p' "$manifest" | head -1)"
if [ -z "$version" ]; then
  echo "no info.version in $manifest" >&2
  exit 1
fi

# The licence files travel in the jar. They are staged from the repository's own copies for the
# build and removed again, so there is one copy to keep right.
staged="$root/resources/META-INF/licenses/metabase-hardcharger-driver"
cleanup() { rm -rf "$root/resources/META-INF"; }
trap cleanup EXIT
mkdir -p "$staged"
cp "$root/LICENSE" "$root/NOTICE" "$root/THIRD-PARTY-NOTICES.md" "$staged/"

built="$metabase/resources/modules/$jar_name"
rm -f "$built"
(cd "$metabase" && ./bin/build-driver.sh hardcharger)
if [ ! -s "$built" ]; then
  echo "the build did not write $built" >&2
  exit 1
fi

mkdir -p "$root/dist"
cp "$built" "$root/dist/$jar_name"

# What must be in the jar. `unzip -Z1` lists names only; each check is exact.
names="$(unzip -Z1 "$root/dist/$jar_name")"
for wanted in \
  metabase/hardcharger/metabase-plugin.yaml \
  metabase/driver/hardcharger__init.class \
  com/gizmodata/hardcharger/jdbc/HardchargerDriver.class \
  com/gizmodata/chargewire/jdbc/ChargewireDriver.class \
  META-INF/NOTICE \
  META-INF/licenses/metabase-hardcharger-driver/LICENSE \
  META-INF/licenses/metabase-hardcharger-driver/NOTICE \
  META-INF/licenses/metabase-hardcharger-driver/THIRD-PARTY-NOTICES.md; do
  if ! grep -qxF -- "$wanted" <<<"$names"; then
    echo "the jar lacks $wanted" >&2
    exit 1
  fi
done
# Metabase provides Clojure and its own namespaces: a jar that carries them would shadow them.
if grep -qE '^(clojure/core|metabase/driver\.clj|metabase/driver__init)' <<<"$names"; then
  echo "the jar contains Clojure or Metabase core classes" >&2
  exit 1
fi
in_jar="$(unzip -p "$root/dist/$jar_name" metabase/hardcharger/metabase-plugin.yaml | sed -n 's/^  version: *//p' | head -1)"
if [ "$in_jar" != "$version" ]; then
  echo "the jar's manifest says version '$in_jar', the repository '$version'" >&2
  exit 1
fi

if command -v sha256sum >/dev/null 2>&1; then
  (cd "$root/dist" && sha256sum "$jar_name" > "$jar_name.sha256")
else
  (cd "$root/dist" && shasum -a 256 "$jar_name" > "$jar_name.sha256")
fi
echo "built dist/$jar_name (driver $version, $(wc -c < "$root/dist/$jar_name" | tr -d ' ') bytes) against Metabase $(tr -d '[:space:]' < "$root/metabase.version")"
cat "$root/dist/$jar_name.sha256"
