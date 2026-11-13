# CLAUDE.md: Metabase Hardcharger driver

A Metabase driver plugin (`:hardcharger`, parent `:sql-jdbc`) for Hardcharger, GizmoData's DuckDB 2.0
server, which speaks the Chargewire protocol. It is built on the Hardcharger JDBC driver
(`com.gizmodata:hardcharger-jdbc`, a thin layer over `chargewire-jdbc`) and generates DuckDB SQL. The
counterpart of `metabase-gizmosql-driver`.

## Layout

```
src/metabase/driver/hardcharger.clj                 the whole driver
resources/metabase/hardcharger/metabase-plugin.yaml  manifest: connection form, init steps, the VERSION
test/metabase/test/data/hardcharger.clj             test extensions: how Metabase's suite loads its datasets
test/metabase/driver/hardcharger_test.clj           the driver's own tests
metabase.version                                    the Metabase tag built and tested against
ci/                                                 scripts that CI and the Makefile both run
e2e/e2e.py, e2e/seed.sql                            end-to-end check: real Metabase (Docker) + real servers
.github/workflows/ci.yml, release.yml               CI, and the release that calls it as its gate
```

## Commands

```bash
make jar      # dist/hardcharger.metabase-driver.jar (+ .sha256), contents checked
make lint     # clj-kondo with Metabase's configuration; a warning fails
make test     # Metabase's driver test suite + own tests, against a server it starts (~6 min on 18 cores)
make test TEST=metabase.driver.hardcharger-test     # one namespace
make e2e      # needs `make jar` first; Docker; writes nothing unless --screenshots is passed
python e2e/e2e.py --jar dist/hardcharger.metabase-driver.jar --screenshots docs/screenshots   # refresh README images
```

Needs JDK 21, the Clojure CLI, uv, Docker, openssl. The first run clones Metabase at `metabase.version` into
`~/.cache/metabase-hardcharger-driver/` (about 700 MB) and downloads every Metabase driver's dependencies.
The driver cannot be built or tested outside a Metabase checkout: `ci/checkout-metabase.sh` links this
repository in as `modules/drivers/hardcharger` and registers it (`ci/patch-metabase.py`).

## Rules that are easy to break

1. **`connection-details->spec` returns only strings and is the same for the same details.** Metabase hashes
   it to decide whether the connection pool is stale; anything that changes per call rebuilds the pool on
   every query.
2. **Nothing but host, port and catalog goes in the JDBC URL.** Credentials and certificates are JDBC
   properties. "Additional JDBC options" is an allowlist (`allowed-additional-options`): never add a property
   that names a program (`oauthBrowser`), a file, or a credential.
3. **Connection-detail keys must not contain a name in `disallowed-connection-parameters`** (Metabase matches
   them as substrings, ignoring case and `-_.`): that is why the keys are `mtls-certificate` and `mtls-key`,
   not `tls-client-certificate` / `tls-client-key`. `disallowed-connection-parameters-test` checks it.
4. **Drivers are handed a Database's Lib metadata or its id**, not a Toucan instance
   (`(lib.metadata/database (mt/metadata-provider))`, `(mt/id)`).
5. **A feature that isn't proven by Metabase's own tests stays declared false.** Metabase hides what a driver
   says it can't do; it fails in front of the user for what a driver wrongly says it can.
   Uploads and table transforms are on, for every connection (Metabase keeps uploads off until an admin
   picks a database and schema; the server refuses a user without the grant, by name). Actions, table
   editing, model persistence and Python transforms are off.
6. **Every opt-out from Metabase's suite is in `test/metabase/test/data/hardcharger.clj` with its reason**, and
   in the README. Don't add one without a Hardcharger, DuckDB or JDBC limitation behind it.
   Two tests are ignored by name in `ci/run-metabase-tests.sh` (they need Metabase's `enterprise/` code).
7. **The driver wraps one Metabase-internal function** (`metabase.sql-tools.sqlglot.core/driver->dialect`)
   so Metabase parses Hardcharger's SQL as DuckDB's; without it transforms cannot be ordered by their SQL.
   The end-to-end check fails if the wrap stops working. Remove it when Metabase lets a driver name its
   dialect.
8. **A JDBC driver gap is reported upstream** (chargewire-jdbc first, then hardcharger-jdbc) with a repro; a
   workaround here names the JDBC version in a comment so it can be removed.

## Releasing

Version: `info.version` in `resources/metabase/hardcharger/metabase-plugin.yaml` (the only place).

1. Move `[Unreleased]` in CHANGELOG.md to `## [X.Y.Z] - YYYY-MM-DD`, set `info.version`, commit.
2. `make check` locally.
3. Push `main`, wait for CI, then push the tag `vX.Y.Z`. `release.yml` checks tag == version and the
   CHANGELOG section, runs all of `ci.yml` on the tagged commit, and attaches the jar and its SHA-256 to a
   GitHub Release whose notes are that CHANGELOG section.
4. A tag whose gate fails before the Release exists is moved to the fixed commit, not burned (nothing else
   publishes this artifact: no Maven, no registry).

After changing `release.yml`, run it once by hand (Actions > Release > Run workflow: a dry run) before tagging.

## Moving to a new Metabase version

Read `docs/developers-guide/driver-changelog.md` at the new tag first. Change `metabase.version`, then
`make jar lint test e2e`. `ci/patch-metabase.py` fails loudly if Metabase moved the lines it anchors on.
Update the README's "Tested versions".
