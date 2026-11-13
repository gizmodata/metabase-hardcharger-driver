#!/usr/bin/env bash
# Start a Hardcharger server, run the test suite against it, and stop the server again (by its pid),
# whatever the tests' result. This is what `make test` runs.
#
#   ci/test-with-server.sh PYTHON METABASE_DIR [PORT] [NAMESPACE]
#
# NAMESPACE runs that one test namespace instead of the whole suite.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="${1:?usage: ci/test-with-server.sh PYTHON METABASE_DIR [PORT] [NAMESPACE]}"
metabase="${2:?usage: ci/test-with-server.sh PYTHON METABASE_DIR [PORT] [NAMESPACE]}"
port="${3:-31347}"
namespace="${4:-}"

if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port $port is taken: choose another (make test TEST_PORT=...)" >&2
  exit 1
fi

mkdir -p "$root/target"
password="$(openssl rand -hex 24)"
export MB_HARDCHARGER_TEST_HOST=127.0.0.1 MB_HARDCHARGER_TEST_PORT="$port"
export MB_HARDCHARGER_TEST_USER=hardcharger MB_HARDCHARGER_TEST_PASSWORD="$password"

stop_server() {
  if [ -f "$root/target/hardcharger.log.pid" ]; then
    kill "$(cat "$root/target/hardcharger.log.pid")" 2>/dev/null || true
    rm -f "$root/target/hardcharger.log.pid"
  fi
}
trap stop_server EXIT
HARDCHARGER_ADMIN_PASSWORD="$password" "$root/ci/start-test-server.sh" "$python" "$root/target/hardcharger.log"

if [ -n "$namespace" ]; then
  CHECK_TEST_RUN_ARGS="--only" "$root/ci/run-metabase-tests.sh" "$metabase" :only "[$namespace]"
else
  "$root/ci/run-metabase-tests.sh" "$metabase"
fi
