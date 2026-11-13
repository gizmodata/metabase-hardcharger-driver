#!/usr/bin/env bash
# Lint the driver and its tests with clj-kondo, using Metabase's own lint configuration (its macros
# and its house rules). A warning fails.
#
#   ci/lint.sh [METABASE_DIR]
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
metabase="${1:-$("$root/ci/checkout-metabase.sh")}"
cd "$metabase"
clojure -M:kondo --fail-level warning --lint modules/drivers/hardcharger/src modules/drivers/hardcharger/test
