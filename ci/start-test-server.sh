#!/usr/bin/env bash
# Start a Hardcharger server for the test suite, in the background, and wait until it answers.
#
#   HARDCHARGER_ADMIN_PASSWORD=... ci/start-test-server.sh PYTHON LOG_FILE
#
# PYTHON is a Python with the `hardcharger` package (e2e/requirements.txt), which downloads the
# released server binary on first use; HARDCHARGER_SERVER_BINARY names another build. The server
# listens on 127.0.0.1, port MB_HARDCHARGER_TEST_PORT (31337), holds its data in memory, and runs in
# UTC, which Metabase's test suite expects of a database server. Its pid is written beside the log.
set -euo pipefail

python="${1:?usage: ci/start-test-server.sh PYTHON LOG_FILE}"
log="${2:?usage: ci/start-test-server.sh PYTHON LOG_FILE}"
port="${MB_HARDCHARGER_TEST_PORT:-31337}"
if [ -z "${HARDCHARGER_ADMIN_PASSWORD:-}" ]; then
  echo "HARDCHARGER_ADMIN_PASSWORD is not set" >&2
  exit 1
fi

TZ=UTC nohup "$python" -m hardcharger server \
  --host 127.0.0.1 --port "$port" --database :memory: --no-web-ui > "$log" 2>&1 < /dev/null &
echo $! > "$log.pid"

for _ in $(seq 1 120); do
  # Any HTTP answer means it is up (the path itself answers 404).
  if curl --silent --output /dev/null --max-time 2 "http://127.0.0.1:$port/chargewire/v1"; then
    echo "Hardcharger is up on 127.0.0.1:$port (pid $(cat "$log.pid"))"
    exit 0
  fi
  if ! kill -0 "$(cat "$log.pid")" 2>/dev/null; then
    echo "the server exited while starting:" >&2
    tail -n 40 "$log" >&2
    exit 1
  fi
  sleep 0.5
done
echo "the server did not answer on 127.0.0.1:$port within 60 s:" >&2
tail -n 40 "$log" >&2
exit 1
