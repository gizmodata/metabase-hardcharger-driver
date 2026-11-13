# Build and test the Metabase Hardcharger driver.
#
#   make jar      dist/hardcharger.metabase-driver.jar, built by Metabase's own driver build
#   make lint     clj-kondo with Metabase's configuration (a warning fails)
#   make test     Metabase's driver test suite + this driver's tests, against a real Hardcharger server
#   make e2e      a real Metabase (Docker) with the jar, against real Hardcharger servers, in a browser
#   make check    all of the above, as CI runs them
#
# Needs JDK 21, the Clojure CLI, Python 3.10+ with uv, Docker and openssl. `make test` starts its own
# server on 127.0.0.1:$(TEST_PORT) and stops it afterwards; TEST=<namespace> runs one namespace.
# The Metabase source checkout (metabase.version) is made once under ~/.cache/metabase-hardcharger-driver.

.PHONY: jar lint test e2e check venv clean

METABASE_VERSION := $(shell tr -d '[:space:]' < metabase.version)
METABASE_DIR ?= $(HOME)/.cache/metabase-hardcharger-driver/metabase-$(METABASE_VERSION)
VENV ?= .venv
PYTHON := $(VENV)/bin/python
TEST_PORT ?= 31347
TEST ?=
JAR := dist/hardcharger.metabase-driver.jar

$(PYTHON): e2e/requirements.txt
	uv venv --python 3.13 $(VENV)
	uv pip install --python $(PYTHON) -r e2e/requirements.txt
	$(PYTHON) -m playwright install chromium

venv: $(PYTHON)

jar:
	ci/checkout-metabase.sh "$(METABASE_DIR)"
	ci/build-jar.sh "$(METABASE_DIR)"

lint:
	ci/checkout-metabase.sh "$(METABASE_DIR)"
	ci/lint.sh "$(METABASE_DIR)"

test: $(PYTHON)
	ci/checkout-metabase.sh "$(METABASE_DIR)"
	ci/test-with-server.sh "$(PYTHON)" "$(METABASE_DIR)" "$(TEST_PORT)" "$(TEST)"

e2e: $(PYTHON)
	@test -f $(JAR) || { echo "no $(JAR): run 'make jar' first"; exit 1; }
	$(PYTHON) e2e/e2e.py --jar $(JAR) --metabase-image metabase/metabase:$(METABASE_VERSION)

check: jar lint test e2e

clean:
	rm -rf dist target resources/META-INF
