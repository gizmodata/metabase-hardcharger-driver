#!/usr/bin/env bash
# Run Metabase's own driver test suite (every test tagged :mb/driver-tests in Metabase's open-source
# test tree) and this driver's tests, against a real Hardcharger server.
#
#   MB_HARDCHARGER_TEST_PASSWORD=... ci/run-metabase-tests.sh [METABASE_DIR] [runner options ...]
#
# The server is named by MB_HARDCHARGER_TEST_HOST (127.0.0.1), _PORT (31337), _USER (hardcharger) and
# _PASSWORD (required). It must be reachable and its administrator must be able to ATTACH catalogs:
# each test dataset becomes an in-memory catalog. Start it with TZ=UTC. Without a server the tests
# fail; nothing is skipped for that.
#
# Runner options are passed to Metabase's test runner, for example
#   :only '[metabase.driver.hardcharger-test]'      one namespace
#   :partition/total 3 :partition/index 0           a third of the suite
# The output goes to target/metabase-tests.log, and ci/check-test-run.py then checks that the run
# was real: enough tests ran, against this driver, with no failure.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -z "${MB_HARDCHARGER_TEST_PASSWORD:-}" ]; then
  echo "MB_HARDCHARGER_TEST_PASSWORD is not set: the tests need a running Hardcharger server" >&2
  exit 1
fi
if [ $# -gt 0 ] && [ -d "$1" ]; then
  metabase="$1"
  shift
else
  metabase="$("$root/ci/checkout-metabase.sh")"
fi

mkdir -p "$root/target"
log="$root/target/metabase-tests.log"
status=0
# The open-source edition only (no :ee alias): Metabase's enterprise/ tree is under its commercial
# licence, and its tests are for features this driver does not declare.
(cd "$metabase" && DRIVERS=hardcharger clojure -X:dev:ci:drivers:drivers-dev:test \
    :only-tags '[:mb/driver-tests]' :exclude-tags '[:mb/transforms-python-test]' "$@") 2>&1 | tee "$log" || status=$?
if [ "$status" -ne 0 ]; then
  echo "the test runner exited with $status" >&2
fi
python3 "$root/ci/check-test-run.py" "$log" ${CHECK_TEST_RUN_ARGS:-} || status=1
exit "$status"
