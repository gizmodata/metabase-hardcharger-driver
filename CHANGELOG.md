# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-11-13

The first release: a Metabase driver for [Hardcharger](https://github.com/gizmodata/hardcharger),
GizmoData's DuckDB 2.0 server, built on the Hardcharger JDBC driver 2.4.4 (Chargewire JDBC driver
2.2.6). Built and tested against Metabase 0.65.4.3 and Hardcharger 2.12.0.

### Added

- The `hardcharger` engine ("Hardcharger" in Metabase's list of databases), as one plugin jar,
  `hardcharger.metabase-driver.jar`, with the JDBC driver inside.
- Connection form: host, port (empty: 31337, or 443 on a GizmoData Cloud host), TLS on by default,
  sign-in with a user name and password or with an access token, the catalog to sync, schema
  filters, a server CA certificate, mutual TLS with a client certificate (which can also be the
  sign-in), and additional JDBC options from an allowlist. Passwords, tokens, certificates and keys
  are stored as Metabase secrets and travel as JDBC properties, never in the URL.
- Sync of tables and views with their comments, columns with their full DuckDB types, primary keys
  and foreign keys, in one query each. Every DuckDB type has a base type (see the README's table).
- The query builder: filters, aggregations (with median, percentile, standard deviation and
  variance), breakouts with every date and time bucket, the week's first day from Metabase's
  setting, relative dates, joins, nested questions, custom columns, regular expressions and text
  functions, date arithmetic and differences, time zone conversion, binning, cumulative and offset
  window functions, and pivot tables in one `GROUPING SETS` statement.
- Native SQL with variables and field filters.
- Metabase's report time zone is the session's `TimeZone`: day and week buckets, filters on a day
  and values with a time zone all follow it.
- Cancelling a question cancels its statement on the server; Metabase's query timeout is sent with
  each statement.
- Connection errors in plain words (wrong credentials, wrong host or port, a server without TLS,
  an untrusted certificate, an unknown catalog), none of which repeats a password or a token.

### Not supported

Declared unsupported, so Metabase hides them instead of failing: CSV uploads, actions and table
editing, model persistence, transforms, connection impersonation, database routing, unfolding of
JSON columns, and dependency tracking of native SQL. The README says why for each.
