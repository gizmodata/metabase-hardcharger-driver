#!/usr/bin/env python3
"""End-to-end check: a real Metabase (Docker) with the driver jar, against real Hardcharger servers.

    python e2e/e2e.py --jar dist/hardcharger.metabase-driver.jar

What it proves, in order (any failure exits non-zero; nothing is skipped):

  1. the plugin loads: Metabase lists the "hardcharger" engine with its connection form;
  2. a database is added through the API, and its sync completes with the right tables, column types,
     primary key and foreign key;
  3. query-builder (MBQL) questions and native SQL with parameters return the exact rows expected;
  4. a report time zone moves day buckets and results as it should (the session's TimeZone);
  5. cancelling a running question cancels its statement on the server;
  6. a dashboard's cards return their rows through the dashboard endpoints, and render in a browser;
  7. connection errors are readable and never contain the password;
  8. TLS with a private CA (uploaded as a secret), and sign-in with a client certificate (mutual TLS);
  9. the additional-options allowlist refuses a dangerous option.

It starts two Hardcharger servers on free ports (plain HTTP with the sample data, and TLS), and one
Metabase container named after this process, and removes what it started. Metabase reaches the servers
on the Docker host as host.docker.internal.

Needs Docker, openssl, and the packages in e2e/requirements.txt (the `hardcharger` package downloads
the server binary; HARDCHARGER_SERVER_BINARY names another build). `playwright install chromium` once.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import http.client
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

import hardcharger

HERE = Path(__file__).resolve().parent
DEFAULT_IMAGE = "metabase/metabase:v0.65.4.3"
ADMIN_EMAIL = "admin@example.invalid"
DOCKER_HOST_NAME = "host.docker.internal"
REPORT_ZONE = "Asia/Kolkata"

# ---- the sample data's formulas (e2e/seed.sql) ----------------------------------------------------

ORDER_COUNT = 1000
STATUSES = ["new", "paid", "shipped", "cancelled"]
REGIONS = ["North", "South", "East", "West"]
EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def order(i: int) -> dict:
    return {
        "id": i,
        "customer_id": (i % 50) + 1,
        "status": STATUSES[i % 4],
        "quantity": (i % 5) + 1,
        "amount": ((i % 7) + 1) * 10,
        "created_at": EPOCH + dt.timedelta(hours=3 * i),
    }


ORDERS = [order(i) for i in range(ORDER_COUNT)]


def customer_region(customer_id: int) -> str:
    return REGIONS[customer_id % 4]


# ---- small helpers ----------------------------------------------------------------------------------


class Failure(AssertionError):
    pass


PASSED: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    if not condition:
        raise Failure(f"{name}: {detail}")
    PASSED.append(name)
    print(f"  ok  {name}", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args=args, check=True, capture_output=True, text=True, **kwargs)


class Metabase:
    """The few API calls this check needs, with the session token of the admin it creates."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.session: str | None = None

    def call(self, method: str, path: str, body: object = None, timeout: float = 120.0, ok=(200, 202)):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url=self.base_url + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if self.session:
            request.add_header("X-Metabase-Session", self.session)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status, text = response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            status, text = error.code, error.read().decode()
        try:
            payload = json.loads(text) if text else None
        except json.JSONDecodeError:
            payload = text
        if ok is not None and status not in ok:
            raise Failure(f"{method} {path} answered {status}: {text[:2000]}")
        return status, payload

    def get(self, path: str, **kwargs):
        return self.call("GET", path, **kwargs)[1]

    def post(self, path: str, body: object = None, **kwargs):
        return self.call("POST", path, body, **kwargs)[1]

    def put(self, path: str, body: object = None, **kwargs):
        return self.call("PUT", path, body, ok=(200, 202, 204), **kwargs)[1]

    def wait_healthy(self, deadline_seconds: float, container: str) -> None:
        deadline = time.monotonic() + deadline_seconds
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(self.base_url + "/api/health", timeout=5) as response:
                    if json.loads(response.read()).get("status") == "ok":
                        return
            except (OSError, ValueError):
                pass
            state = subprocess.run(
                args=["docker", "inspect", "-f", "{{.State.Running}}", container], capture_output=True, text=True
            )
            if state.stdout.strip() != "true":
                raise Failure(f"the Metabase container stopped while starting: {state.stderr.strip()}")
            time.sleep(3)
        raise Failure(f"Metabase did not become healthy within {deadline_seconds:.0f} s")

    def set_up(self, password: str) -> None:
        token = self.get("/api/session/properties")["setup-token"]
        answer = self.post(
            "/api/setup",
            {
                "token": token,
                "user": {
                    "first_name": "Ada",
                    "last_name": "Admin",
                    "email": ADMIN_EMAIL,
                    "password": password,
                    "site_name": "Hardcharger e2e",
                },
                "prefs": {"site_name": "Hardcharger e2e", "site_locale": "en", "allow_tracking": False},
            },
        )
        self.session = answer["id"]

    def dataset(self, query: dict, timeout: float = 120.0) -> dict:
        answer = self.post("/api/dataset", query, timeout=timeout)
        if answer.get("status") != "completed":
            raise Failure(f"query did not complete: {json.dumps(answer)[:3000]}")
        return answer

    def rows(self, query: dict) -> list[list]:
        return self.dataset(query)["data"]["rows"]


def database_details(server: hardcharger.Server, **extra) -> dict:
    return {
        "host": DOCKER_HOST_NAME,
        "port": server.port,
        "tls": False,
        "use-token": False,
        "user": server.username,
        "password": server.password,
        **extra,
    }


def wait_for_sync(mb: Metabase, database_id: int, deadline_seconds: float = 180.0) -> dict:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        database = mb.get(f"/api/database/{database_id}")
        if database.get("initial_sync_status") == "complete":
            return database
        if database.get("initial_sync_status") == "aborted":
            raise Failure("the database's first sync was aborted")
        time.sleep(2)
    raise Failure(f"sync of database {database_id} did not complete within {deadline_seconds:.0f} s")


def uploaded(pem_path: Path) -> str:
    """A secret's value the way the admin form uploads a file."""
    return "data:application/octet-stream;base64," + base64.b64encode(pem_path.read_bytes()).decode()


# ---- certificates for the TLS server ------------------------------------------------------------------


def make_certificates(directory: Path) -> dict[str, Path]:
    """A throwaway CA, a server certificate for the names Metabase and this script dial, and a client
    certificate whose e-mail SAN is the identity it signs in as."""
    ca_key, ca = directory / "ca-key.pem", directory / "ca.pem"
    run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=e2e test CA",
         "-keyout", str(ca_key), "-out", str(ca)])

    def issue(name: str, subject: str, extensions: str) -> tuple[Path, Path]:
        key, request, certificate = directory / f"{name}-key.pem", directory / f"{name}.csr", directory / f"{name}.pem"
        extension_file = directory / f"{name}.ext"
        extension_file.write_text(extensions)
        run(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", subject,
             "-keyout", str(key), "-out", str(request)])
        run(["openssl", "x509", "-req", "-in", str(request), "-CA", str(ca), "-CAkey", str(ca_key),
             "-CAcreateserial", "-days", "2", "-extfile", str(extension_file), "-out", str(certificate)])
        return certificate, key

    server, server_key = issue(
        "server", "/CN=hardcharger e2e",
        f"subjectAltName=DNS:{DOCKER_HOST_NAME},DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n",
    )
    client, client_key = issue(
        "client", "/CN=metabase e2e",
        "subjectAltName=email:metabase@example.invalid\nextendedKeyUsage=clientAuth\n",
    )
    return {"ca": ca, "server": server, "server_key": server_key, "client": client, "client_key": client_key}


# ---- the checks ---------------------------------------------------------------------------------------


def check_plugin(mb: Metabase) -> None:
    print("1. plugin")
    engines = mb.get("/api/session/properties")["engines"]
    check("the hardcharger engine is registered", "hardcharger" in engines, sorted(engines))
    engine = engines["hardcharger"]
    check("its display name is Hardcharger", engine.get("driver-name") == "Hardcharger", engine.get("driver-name"))
    names = [p.get("name") for p in engine["details-fields"]]
    for wanted in ["host", "port", "tls", "use-token", "user", "password", "catalog", "additional-options"]:
        check(f"the connection form has {wanted}", wanted in names, names)
    secret_like = {p.get("name"): p.get("type") for p in engine["details-fields"]}
    check("the password field is a password", secret_like.get("password") == "password", secret_like)
    token_fields = [n for n in names if n and n.startswith("token")]
    check("the access token is a secret (expanded into token-* fields)", "token-value" in token_fields, token_fields)
    check("the CA certificate is a secret", "tls-ca-certificate-value" in names, names)


def add_database(mb: Metabase, server: hardcharger.Server) -> tuple[int, dict, dict]:
    print("2. add the database and sync")
    database = mb.post(
        "/api/database",
        {"engine": "hardcharger", "name": "Hardcharger shop", "details": database_details(server, catalog="memory")},
    )
    database_id = database["id"]
    stored = json.dumps(mb.get(f"/api/database/{database_id}"))
    check("the stored password is not returned by the API", server.password not in stored)
    synced = wait_for_sync(mb, database_id)
    check("the server's time zone was read", synced.get("timezone") == "UTC", synced.get("timezone"))
    check("the server's version was read", "2." in json.dumps(synced.get("dbms_version")), synced.get("dbms_version"))

    metadata = mb.get(f"/api/database/{database_id}/metadata?include_hidden=true")
    tables = {(t["schema"], t["name"]): t for t in metadata["tables"]}
    check(
        "tables and the view are synced, in their schema",
        {("shop", "customers"), ("shop", "orders"), ("shop", "paid_orders"), ("shop", "all_types")} <= set(tables),
        sorted(tables),
    )
    check("no system schema is synced", all(s not in ("information_schema", "pg_catalog") for s, _ in tables))
    check("the table comment is its description",
          tables[("shop", "customers")].get("description") == "People who buy things",
          tables[("shop", "customers")].get("description"))

    fields = {key: {f["name"]: f for f in table["fields"]} for key, table in tables.items()}
    orders, customers = fields[("shop", "orders")], fields[("shop", "customers")]
    check("orders.id is the primary key", orders["id"]["semantic_type"] == "type/PK", orders["id"]["semantic_type"])
    check("orders.customer_id is a foreign key to customers.id",
          orders["customer_id"]["semantic_type"] == "type/FK"
          and orders["customer_id"]["fk_target_field_id"] == customers["id"]["id"],
          (orders["customer_id"]["semantic_type"], orders["customer_id"]["fk_target_field_id"]))
    check("the column comment is its description", customers["region"].get("description") == "Sales region",
          customers["region"].get("description"))

    expected_types = {
        "c_bool": "type/Boolean", "c_tinyint": "type/Integer", "c_smallint": "type/Integer", "c_int": "type/Integer",
        "c_bigint": "type/BigInteger", "c_hugeint": "type/BigInteger", "c_utinyint": "type/Integer",
        "c_usmallint": "type/Integer", "c_uint": "type/BigInteger", "c_ubigint": "type/BigInteger",
        "c_uhugeint": "type/BigInteger", "c_float": "type/Float", "c_double": "type/Float",
        "c_dec": "type/Decimal", "c_dec0": "type/BigInteger", "c_varchar": "type/Text", "c_blob": "type/*",
        "c_uuid": "type/UUID", "c_date": "type/Date", "c_time": "type/Time", "c_timetz": "type/Time",
        "c_ts": "type/DateTime", "c_ts_s": "type/DateTime", "c_ts_ms": "type/DateTime", "c_ts_ns": "type/DateTime",
        "c_tstz": "type/DateTimeWithLocalTZ", "c_interval": "type/*", "c_enum": "type/Text",
        "c_list": "type/Array", "c_struct": "type/Dictionary", "c_map": "type/Dictionary", "c_json": "type/JSON",
        "c_variant": "type/*",
    }
    actual_types = {name: f["base_type"] for name, f in fields[("shop", "all_types")].items()}
    check("every DuckDB type has its base type", actual_types == expected_types,
          {k: (actual_types.get(k), v) for k, v in expected_types.items() if actual_types.get(k) != v})
    check("a DECIMAL keeps its precision and scale as the database type",
          fields[("shop", "all_types")]["c_dec"]["database_type"] == "DECIMAL(18,3)",
          fields[("shop", "all_types")]["c_dec"]["database_type"])

    table_ids = {name: table["id"] for (_, name), table in tables.items()}
    field_ids = {f"{name}.{f}": field["id"] for (_, name), fs in fields.items() for f, field in fs.items()}
    return database_id, table_ids, field_ids


def check_queries(mb: Metabase, database_id: int, tables: dict, fields: dict) -> None:
    print("3. queries")

    def mbql(query: dict) -> list[list]:
        return mb.rows({"database": database_id, "type": "query", "query": query})

    def native(sql: str, **extra) -> dict:
        return mb.dataset({"database": database_id, "type": "native", "native": {"query": sql, **extra}})

    rows = mbql({"source-table": tables["orders"], "aggregation": [["count"]]})
    check("count of orders", rows == [[ORDER_COUNT]], rows)

    by_status = Counter(o["status"] for o in ORDERS if o["quantity"] >= 3)
    amount_by_status = Counter()
    for o in ORDERS:
        if o["quantity"] >= 3:
            amount_by_status[o["status"]] += o["amount"]
    rows = mbql({
        "source-table": tables["orders"],
        "aggregation": [["count"], ["sum", ["field", fields["orders.amount"], None]]],
        "breakout": [["field", fields["orders.status"], None]],
        "filter": [">=", ["field", fields["orders.quantity"], None], 3],
        "order-by": [["asc", ["field", fields["orders.status"], None]]],
    })
    expected = [[s, by_status[s], amount_by_status[s]] for s in sorted(by_status)]
    check("filter, breakout, count and sum", [[r[0], r[1], float(r[2])] for r in rows] == expected, (rows, expected))

    # an implicit join through the synced foreign key
    by_region = Counter(customer_region(o["customer_id"]) for o in ORDERS)
    rows = mbql({
        "source-table": tables["orders"],
        "aggregation": [["count"]],
        "breakout": [["field", fields["customers.region"], {"source-field": fields["orders.customer_id"]}]],
    })
    check("breakout by a joined table's column (foreign key)", sorted(map(tuple, rows)) == sorted(by_region.items()),
          (rows, by_region))

    by_month = Counter(o["created_at"].strftime("%Y-%m") for o in ORDERS)
    rows = mbql({
        "source-table": tables["orders"],
        "aggregation": [["count"]],
        "breakout": [["field", fields["orders.created_at"], {"temporal-unit": "month"}]],
    })
    check("breakout by month (report time zone unset: UTC)",
          {r[0][:7]: r[1] for r in rows} == dict(by_month), (rows, by_month))

    rows = mbql({
        "source-table": tables["orders"],
        "expressions": {"double": ["*", ["field", fields["orders.amount"], None], 2]},
        "fields": [["field", fields["orders.id"], None], ["expression", "double"]],
        "filter": ["=", ["field", fields["orders.id"], None], 6],
    })
    check("a custom column", [[r[0], float(r[1])] for r in rows] == [[6, 140.0]], rows)

    rows = mbql({
        "source-table": tables["orders"],
        "aggregation": [["median", ["field", fields["orders.amount"], None]],
                        ["stddev", ["field", fields["orders.quantity"], None]]],
    })
    amounts = sorted(o["amount"] for o in ORDERS)
    median = (amounts[499] + amounts[500]) / 2
    check("median and standard deviation", float(rows[0][0]) == median and 1.41 < float(rows[0][1]) < 1.42, rows)

    rows = mbql({"source-table": tables["paid_orders"], "aggregation": [["count"]]})
    check("a view is queryable", rows == [[sum(1 for o in ORDERS if o["status"] == "paid")]], rows)

    answer = native("SELECT id, status, amount, created_at FROM shop.orders WHERE id = 7")
    check("native SQL", answer["data"]["rows"] == [[7, "shipped", 10.0, "2026-01-01T21:00:00Z"]]
          or [[r[0], r[1], float(r[2]), r[3]] for r in answer["data"]["rows"]] == [[7, "shipped", 10.0, "2026-01-01T21:00:00Z"]],
          answer["data"]["rows"])

    answer = mb.dataset({
        "database": database_id, "type": "native",
        "native": {
            "query": "SELECT count(*) AS n FROM shop.orders WHERE status = {{status}} AND quantity >= {{quantity}}",
            "template-tags": {
                "status": {"id": "a1", "name": "status", "display-name": "Status", "type": "text"},
                "quantity": {"id": "a2", "name": "quantity", "display-name": "Quantity", "type": "number"},
            },
        },
        "parameters": [
            {"type": "category", "target": ["variable", ["template-tag", "status"]], "value": "paid"},
            {"type": "number", "target": ["variable", ["template-tag", "quantity"]], "value": "4"},
        ],
    })
    expected_n = sum(1 for o in ORDERS if o["status"] == "paid" and o["quantity"] >= 4)
    check("native SQL with a text and a number parameter", answer["data"]["rows"] == [[expected_n]],
          (answer["data"]["rows"], expected_n))

    answer = mb.dataset({
        "database": database_id, "type": "native",
        "native": {
            "query": "SELECT count(*) AS n FROM shop.orders WHERE {{created}} AND {{status}}",
            "template-tags": {
                "created": {"id": "b1", "name": "created", "display-name": "Created", "type": "dimension",
                            "dimension": ["field", fields["orders.created_at"], None], "widget-type": "date/range"},
                "status": {"id": "b2", "name": "status", "display-name": "Status", "type": "dimension",
                           "dimension": ["field", fields["orders.status"], None], "widget-type": "string/="},
            },
        },
        "parameters": [
            {"type": "date/range", "target": ["dimension", ["template-tag", "created"]],
             "value": "2026-02-01~2026-02-28"},
            {"type": "string/=", "target": ["dimension", ["template-tag", "status"]], "value": ["new", "paid"]},
        ],
    })
    expected_n = sum(1 for o in ORDERS
                     if dt.date(2026, 2, 1) <= o["created_at"].date() <= dt.date(2026, 2, 28)
                     and o["status"] in ("new", "paid"))
    check("native SQL with a date-range and a list field filter", answer["data"]["rows"] == [[expected_n]],
          (answer["data"]["rows"], expected_n))

    answer = native("SELECT * FROM shop.all_types")
    row = dict(zip([c["name"] for c in answer["data"]["cols"]], answer["data"]["rows"][0]))
    check("values of every type come back readable", (
        row["c_bool"] is True and row["c_hugeint"] == 12345678901234567890123456789
        and row["c_ubigint"] == 18446744073709551615 and float(row["c_dec"]) == 12345.678 and row["c_dec0"] == 42
        and row["c_uuid"] == "123e4567-e89b-12d3-a456-426614174000" and row["c_date"].startswith("2026-11-13")
        and row["c_time"].startswith("13:14:15.123456") and row["c_ts"].startswith("2026-11-13T13:14:15.123456")
        and row["c_ts_ns"].startswith("2026-11-13T13:14:15.123456") and row["c_tstz"] == "2026-11-13T13:14:15.123456Z"
        and row["c_interval"] == "P1Y2M3DT4H" and row["c_enum"] == "ok" and row["c_list"] == "[1, 2, 3]"
        and json.loads(row["c_struct"]) == {"a": 1, "b": "x"} and json.loads(row["c_map"]) == {"k": 1}
        and json.loads(row["c_json"]) == {"a": [1, 2]}), row)

    status, answer = mb.call("POST", "/api/dataset",
                             {"database": database_id, "type": "native", "native": {"query": "SELEC 1"}}, ok=None)
    error_text = json.dumps(answer)
    check("a SQL error is the server's message", "syntax error at or near" in error_text, error_text[:600])


def check_report_time_zone(mb: Metabase, database_id: int, tables: dict, fields: dict) -> None:
    print("4. report time zone")
    mb.put("/api/setting/report-timezone", {"value": REPORT_ZONE})
    try:
        zone = ZoneInfo(REPORT_ZONE)
        by_day = Counter(o["created_at"].astimezone(zone).date().isoformat() for o in ORDERS)
        rows = mb.rows({
            "database": database_id, "type": "query",
            "query": {"source-table": tables["orders"], "aggregation": [["count"]],
                      "breakout": [["field", fields["orders.created_at"], {"temporal-unit": "day"}]]},
        })
        check(f"day buckets are {REPORT_ZONE} days", {r[0][:10]: r[1] for r in rows} == dict(by_day),
              (rows[:3], sorted(by_day.items())[:3]))
        check("bucket values carry the zone's offset", all(r[0].endswith("+05:30") for r in rows), rows[:2])
        # 8 orders a day in UTC; in +05:30 the first local day (2026-01-01) starts 5.5 h early, so it holds 7
        check("the first local day differs from the UTC one", by_day["2026-01-01"] == 7 and rows[0][1] == 7, rows[0])

        answer = mb.dataset({"database": database_id, "type": "native",
                             "native": {"query": "SELECT current_setting('TimeZone') AS zone, created_at "
                                                 "FROM shop.orders WHERE id = 0"}})
        check("the session's TimeZone is the report time zone",
              answer["data"]["rows"] == [[REPORT_ZONE, "2026-01-01T05:30:00+05:30"]], answer["data"]["rows"])
        check("results say which zone they are in", answer["data"].get("results_timezone") == REPORT_ZONE,
              answer["data"].get("results_timezone"))

        # a filter on a local day: 2026-01-02 in the report zone is 8 orders whose UTC days differ
        rows = mb.rows({
            "database": database_id, "type": "query",
            "query": {"source-table": tables["orders"], "aggregation": [["count"]],
                      "filter": ["=", ["field", fields["orders.created_at"], {"temporal-unit": "day"}], "2026-01-02"]},
        })
        check("a filter on a day means that day in the report zone", rows == [[by_day["2026-01-02"]]], rows)
    finally:
        mb.put("/api/setting/report-timezone", {"value": None})
    answer = mb.dataset({"database": database_id, "type": "native",
                         "native": {"query": "SELECT current_setting('TimeZone') AS zone"}})
    check("without a report time zone the session is back in the server's zone",
          answer["data"]["rows"] == [["UTC"]], answer["data"]["rows"])


def check_cancel(mb: Metabase, database_id: int, server_log: Path) -> None:
    print("5. cancel")
    marker = f"cancel_probe_{secrets.token_hex(4)}"
    slow = f"SELECT count(*) AS {marker} FROM range(100000000000) a, range(1000) b"
    body = json.dumps({"database": database_id, "type": "native", "native": {"query": slow}}).encode()
    host, port = mb.base_url.removeprefix("http://").split(":")
    connection = http.client.HTTPConnection(host=host, port=int(port), timeout=60)
    connection.request("POST", "/api/dataset", body=body,
                       headers={"Content-Type": "application/json", "X-Metabase-Session": mb.session})

    def log_has(*needles: str) -> bool:
        text = server_log.read_text(errors="replace")
        return any(all(n in line for n in needles) for line in text.splitlines())

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not log_has(marker):
        time.sleep(0.2)
    check("the slow statement reached the server", log_has(marker))
    time.sleep(1.0)
    cancelled_before = server_log.read_text(errors="replace").count("statement cancelled")
    # what a browser does when the person presses Cancel: it drops the request
    connection.sock.shutdown(socket.SHUT_RDWR)
    connection.close()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if server_log.read_text(errors="replace").count("statement cancelled") > cancelled_before:
            break
        time.sleep(0.2)
    text = server_log.read_text(errors="replace")
    check("the server cancelled the statement after the request was dropped",
          text.count("statement cancelled") > cancelled_before, "no 'statement cancelled' line in the server log")
    last = [line for line in text.splitlines() if "statement cancelled" in line][-1]
    check("it was cancelled by the client, not by a timeout", "reason=client" in last, last)
    rows = mb.rows({"database": database_id, "type": "native", "native": {"query": "SELECT 42 AS answer"}})
    check("the next question runs", rows == [[42]], rows)


def make_dashboard(mb: Metabase, database_id: int, tables: dict, fields: dict) -> tuple[int, dict]:
    print("6. dashboard")
    by_month = mb.post("/api/card", {
        "name": "Orders by month", "display": "bar", "visualization_settings": {},
        "dataset_query": {"database": database_id, "type": "query", "query": {
            "source-table": tables["orders"], "aggregation": [["count"]],
            "breakout": [["field", fields["orders.created_at"], {"temporal-unit": "month"}]]}},
    })
    by_region = mb.post("/api/card", {
        "name": "Revenue by region", "display": "table", "visualization_settings": {},
        "dataset_query": {"database": database_id, "type": "native", "native": {
            "query": "SELECT c.region, sum(o.amount) AS revenue, count(*) AS orders\n"
                     "FROM shop.orders o JOIN shop.customers c ON c.id = o.customer_id\n"
                     "GROUP BY c.region ORDER BY c.region"}},
    })
    total = mb.post("/api/card", {
        "name": "Paid orders", "display": "scalar", "visualization_settings": {},
        "dataset_query": {"database": database_id, "type": "query", "query": {
            "source-table": tables["orders"], "aggregation": [["count"]],
            "filter": ["=", ["field", fields["orders.status"], None], "paid"]}},
    })
    dashboard = mb.post("/api/dashboard", {"name": "Hardcharger shop"})
    mb.put(f"/api/dashboard/{dashboard['id']}", {"dashcards": [
        {"id": -1, "card_id": total["id"], "row": 0, "col": 0, "size_x": 6, "size_y": 4},
        {"id": -2, "card_id": by_region["id"], "row": 0, "col": 6, "size_x": 18, "size_y": 4},
        {"id": -3, "card_id": by_month["id"], "row": 4, "col": 0, "size_x": 24, "size_y": 7},
    ]})
    saved = mb.get(f"/api/dashboard/{dashboard['id']}")
    check("the dashboard has three cards", len(saved["dashcards"]) == 3, len(saved["dashcards"]))

    results = {}
    for dashcard in saved["dashcards"]:
        answer = mb.post(f"/api/dashboard/{dashboard['id']}/dashcard/{dashcard['id']}/card/{dashcard['card_id']}/query")
        check(f"dashboard card \"{dashcard['card']['name']}\" completed", answer.get("status") == "completed",
              json.dumps(answer)[:1500])
        results[dashcard["card"]["name"]] = answer["data"]["rows"]

    check("the scalar card's value", results["Paid orders"] == [[250]], results["Paid orders"])
    revenue, counts = Counter(), Counter()
    for o in ORDERS:
        revenue[customer_region(o["customer_id"])] += o["amount"]
        counts[customer_region(o["customer_id"])] += 1
    check("the native card's rows",
          [[r[0], float(r[1]), r[2]] for r in results["Revenue by region"]]
          == [[region, float(revenue[region]), counts[region]] for region in sorted(revenue)],
          results["Revenue by region"])
    by_month_expected = Counter(o["created_at"].strftime("%Y-%m") for o in ORDERS)
    check("the chart card's rows", {r[0][:7]: r[1] for r in results["Orders by month"]} == dict(by_month_expected),
          results["Orders by month"])
    return dashboard["id"], {"by_month": by_month["id"], "by_region": by_region["id"], "total": total["id"]}


def check_in_a_browser(mb: Metabase, password: str, dashboard_id: int, cards: dict, screenshots: Path | None) -> None:
    print("6b. in a browser")
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page = context.new_page()
        problems: list[str] = []
        page.on("pageerror", lambda error: problems.append(f"page error: {error}"))
        page.on("response", lambda response: problems.append(f"{response.status} {response.url}")
                if response.status >= 500 else None)

        page.goto(mb.base_url + "/auth/login")
        page.get_by_label("Email address").fill(ADMIN_EMAIL)
        page.get_by_label("Password").fill(password)
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_url(lambda url: "/auth/login" not in url, timeout=30_000)

        page.goto(f"{mb.base_url}/dashboard/{dashboard_id}")
        expect(page.get_by_text("Hardcharger shop").first).to_be_visible(timeout=30_000)
        for title in ["Paid orders", "Revenue by region", "Orders by month"]:
            expect(page.get_by_text(title, exact=True).first).to_be_visible(timeout=30_000)
        expect(page.get_by_text("250", exact=True).first).to_be_visible(timeout=30_000)
        expect(page.get_by_text("North", exact=True).first).to_be_visible(timeout=30_000)
        # the bar chart drew its bars
        expect(page.locator("svg path, canvas").first).to_be_visible(timeout=30_000)
        page.wait_for_load_state("networkidle")
        check("the dashboard renders its three cards", True)
        errors = page.get_by_text("There was a problem displaying this chart").count()
        check("no card shows an error", errors == 0, errors)
        if screenshots:
            page.screenshot(path=str(screenshots / "dashboard.png"))

        page.goto(f"{mb.base_url}/question/{cards['by_month']}")
        expect(page.get_by_text("Orders by month").first).to_be_visible(timeout=30_000)
        page.wait_for_load_state("networkidle")
        expect(page.get_by_text("Count", exact=True).first).to_be_visible(timeout=30_000)
        check("a saved question renders", True)
        if screenshots:
            page.screenshot(path=str(screenshots / "question.png"))

        page.goto(f"{mb.base_url}/admin/databases/create")
        page.wait_for_load_state("networkidle")
        # choose the engine; the list is a searchable picker
        search = page.get_by_placeholder("Search databases")
        if search.count():
            search.first.fill("Hardcharger")
        page.get_by_text("Hardcharger", exact=True).first.click()
        expect(page.get_by_text("Use a secure connection (TLS)").first).to_be_visible(timeout=30_000)
        expect(page.get_by_text("Sign in with an access token").first).to_be_visible()
        check("the connection form shows the driver's fields", True)
        if screenshots:
            page.screenshot(path=str(screenshots / "connection-form.png"), full_page=True)

        check("the browser saw no page error and no 5xx response", not problems, problems[:5])
        browser.close()


def check_errors(mb: Metabase, server: hardcharger.Server, tls_server: hardcharger.Server) -> None:
    print("7. connection errors")

    def refused(details: dict) -> str:
        status, answer = mb.call("POST", "/api/database",
                                 {"engine": "hardcharger", "name": "should not be saved", "details": details}, ok=None)
        check("a connection that cannot work is refused", status == 400, (status, answer))
        return json.dumps(answer)

    wrong_password = "wrong-" + secrets.token_urlsafe(12)
    text = refused(database_details(server, password=wrong_password))
    check("a wrong password says so", "username or password" in text.lower() or "user name or password" in text.lower(), text)
    check("the error does not repeat the password", wrong_password not in text, text)

    text = refused(database_details(server, port=free_port()))
    check("a closed port says to check host and port", "host and port" in text.lower() or "port" in text.lower(), text)
    check("that error does not contain the password either", server.password not in text, text)

    text = refused(database_details(server, tls=True))
    check("TLS against a plain server says the server does not use TLS", "does not use TLS" in text, text)

    text = refused(database_details(tls_server, tls=True))
    check("an untrusted certificate says how to trust it", "Server CA certificate" in text, text)

    text = refused(database_details(server, catalog="no_such_catalog"))
    check("an unknown catalog says so", "no catalog of that name" in text, text)

    print("9. additional options")
    text = refused({**database_details(server), "additional-options": "oauthBrowser=/usr/bin/touch /tmp/pwned"})
    check("an option that names a program is refused", "oauthBrowser" in text or "not allowed" in text.lower()
          or "disallowed" in text.lower(), text)
    text = refused({**database_details(server), "additional-options": "tlsRootCerts=/etc/passwd"})
    check("an option that names a file is refused", "tlsRootCerts" in text or "not allowed" in text.lower()
          or "disallowed" in text.lower(), text)
    text = refused({**database_details(server), "additional-options": "resultPartitions=abc"})
    check("a bad value of an allowed option is the JDBC driver's message", "resultPartitions" in text, text)


def check_tls(mb: Metabase, tls_server: hardcharger.Server, certificates: dict[str, Path]) -> None:
    print("8. TLS and mutual TLS")
    database = mb.post("/api/database", {
        "engine": "hardcharger", "name": "Hardcharger over TLS",
        "details": database_details(
            tls_server, tls=True, catalog="memory",
            **{"tls-ca-certificate-options": "uploaded", "tls-ca-certificate-value": uploaded(certificates["ca"])}),
    })
    wait_for_sync(mb, database["id"])
    rows = mb.rows({"database": database["id"], "type": "native",
                    "native": {"query": "SELECT HARDCHARGER_USER() AS who, 1 + 1 AS two"}})
    check("TLS with an uploaded private CA", rows == [[tls_server.username, 2]], rows)
    stored = json.dumps(mb.get(f"/api/database/{database['id']}"))
    check("the stored certificate is not returned in clear",
          base64.b64encode(certificates["ca"].read_bytes()).decode()[:60] not in stored)

    database = mb.post("/api/database", {
        "engine": "hardcharger", "name": "Hardcharger with a client certificate",
        "details": {
            "host": DOCKER_HOST_NAME, "port": tls_server.port, "tls": True, "use-token": False,
            "mtls-sign-in": True, "advanced-options": True,
            "tls-ca-certificate-options": "uploaded", "tls-ca-certificate-value": uploaded(certificates["ca"]),
            "mtls-certificate-options": "uploaded", "mtls-certificate-value": uploaded(certificates["client"]),
            "mtls-key-options": "uploaded", "mtls-key-value": uploaded(certificates["client_key"]),
        },
    })
    rows = mb.rows({"database": database["id"], "type": "native",
                    "native": {"query": "SELECT HARDCHARGER_USER() AS who"}})
    check("sign-in with the client certificate (its e-mail is the user)", rows == [["metabase@example.invalid"]], rows)


def check_access_token(mb: Metabase, server: hardcharger.Server) -> None:
    print("8b. access token")
    request = urllib.request.Request(
        url=f"http://127.0.0.1:{server.port}/chargewire/v1/auth/token",
        data=json.dumps({"grant_type": "password", "username": server.username, "password": server.password}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        token = json.loads(response.read())["access_token"]
    database = mb.post("/api/database", {
        "engine": "hardcharger", "name": "Hardcharger with a token",
        "details": {"host": DOCKER_HOST_NAME, "port": server.port, "tls": False, "use-token": True,
                    "token-value": token, "catalog": "memory"},
    })
    rows = mb.rows({"database": database["id"], "type": "native", "native": {"query": "SELECT HARDCHARGER_USER()"}})
    check("sign-in with an access token", rows == [[server.username]], rows)
    stored = json.dumps(mb.get(f"/api/database/{database['id']}"))
    check("the stored token is not returned by the API", token not in stored)


# ---- main ---------------------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jar", required=True, type=Path, help="the driver plugin jar to test")
    parser.add_argument("--metabase-image", default=os.environ.get("METABASE_IMAGE", DEFAULT_IMAGE))
    parser.add_argument("--screenshots", type=Path, help="directory to write the README's screenshots to")
    parser.add_argument("--keep", action="store_true", help="leave Metabase and the servers running at the end")
    parser.add_argument("--no-browser", action="store_true", help="skip the browser step (not for the release gate)")
    args = parser.parse_args()

    jar = args.jar.resolve()
    if not jar.is_file():
        print(f"no jar at {jar}", file=sys.stderr)
        return 2
    for tool in ("docker", "openssl"):
        if shutil.which(tool) is None:
            print(f"{tool} is needed and was not found", file=sys.stderr)
            return 2
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)

    work = Path(tempfile.mkdtemp(prefix="mbhc-e2e-"))
    plugins = work / "plugins"
    plugins.mkdir()
    shutil.copy(jar, plugins / "hardcharger.metabase-driver.jar")
    os.chmod(work, 0o755)
    os.chmod(plugins, 0o777)  # the container's user writes the bundled drivers here too
    certificates = make_certificates(work)
    server_log, tls_log = work / "hardcharger.log", work / "hardcharger-tls.log"
    container = f"mbhc-e2e-{os.getpid()}"
    metabase_port = free_port()
    admin_password = "Aa1!" + secrets.token_urlsafe(18)
    binary = os.environ.get("HARDCHARGER_SERVER_BINARY")
    started_container = False
    failed = True

    # 0.0.0.0: the Metabase container reaches the servers through the Docker host's gateway address.
    server = hardcharger.Server(
        host="0.0.0.0", binary=binary, log_file=server_log, env={"TZ": "UTC"},
        extra_args=["--init-sql-commands-file", str(HERE / "seed.sql"), "--no-web-ui", "--print-queries"],
    )
    tls_server = hardcharger.Server(
        host="0.0.0.0", binary=binary, log_file=tls_log, env={"TZ": "UTC"},
        tls_cert=certificates["server"], tls_key=certificates["server_key"],
        extra_args=["--no-web-ui", "--mtls-ca-cert", str(certificates["ca"]), "--mtls-certificate-sign-in",
                    "--mtls-admin-identities", "metabase@example.invalid"],
    )
    mb = Metabase(base_url=f"http://127.0.0.1:{metabase_port}")
    try:
        server.start()
        tls_server.start()
        print(f"Hardcharger on :{server.port} (plain) and :{tls_server.port} (TLS); starting {args.metabase_image}")
        run([
            "docker", "run", "-d", "--name", container,
            "--add-host", f"{DOCKER_HOST_NAME}:host-gateway",
            "-p", f"127.0.0.1:{metabase_port}:3000",
            "-v", f"{plugins}:/plugins",
            "-e", "MB_PLUGINS_DIR=/plugins",
            "-e", "MB_CHECK_FOR_UPDATES=false", "-e", "MB_ANON_TRACKING_ENABLED=false",
            "-e", "JAVA_TIMEZONE=UTC",
            args.metabase_image,
        ])
        started_container = True
        mb.wait_healthy(deadline_seconds=600, container=container)
        version = mb.get("/api/session/properties")["version"]
        print(f"Metabase {version.get('tag')} is up on {mb.base_url}")
        mb.set_up(password=admin_password)

        check_plugin(mb)
        database_id, tables, fields = add_database(mb, server)
        check_queries(mb, database_id, tables, fields)
        check_report_time_zone(mb, database_id, tables, fields)
        check_cancel(mb, database_id, server_log)
        dashboard_id, cards = make_dashboard(mb, database_id, tables, fields)
        if args.no_browser:
            print("6b. in a browser: NOT RUN (--no-browser)")
        else:
            check_in_a_browser(mb, admin_password, dashboard_id, cards, args.screenshots)
        check_errors(mb, server, tls_server)
        check_tls(mb, tls_server, certificates)
        check_access_token(mb, server)

        logs = subprocess.run(args=["docker", "logs", container], capture_output=True, text=True)
        log_text = logs.stdout + logs.stderr
        for secret_value, what in [(server.password, "the database password"), (tls_server.password, "the TLS server's password")]:
            check(f"Metabase's log does not contain {what}", secret_value not in log_text)
        for log_path in (server_log, tls_log):
            check(f"{log_path.name} does not contain a password",
                  server.password not in log_path.read_text(errors="replace")
                  and tls_server.password not in log_path.read_text(errors="replace"))

        failed = False
        print(f"\nPASSED: {len(PASSED)} checks against Metabase {version.get('tag')} and Hardcharger "
              f"{hardcharger.__version__ if hasattr(hardcharger, '__version__') else ''}".rstrip())
        if args.no_browser:
            print("(the browser step was not run)")
        return 0
    except Failure as failure:
        print(f"\nFAILED after {len(PASSED)} checks: {failure}", file=sys.stderr)
        return 1
    finally:
        if failed and started_container:
            logs = subprocess.run(args=["docker", "logs", "--tail", "120", container], capture_output=True, text=True)
            print("---- last Metabase log lines ----", file=sys.stderr)
            print((logs.stdout + logs.stderr)[-12000:], file=sys.stderr)
            for log_path in (server_log, tls_log):
                if log_path.exists():
                    print(f"---- {log_path.name} (tail) ----", file=sys.stderr)
                    print("\n".join(log_path.read_text(errors="replace").splitlines()[-25:]), file=sys.stderr)
        if args.keep:
            print(f"left running: container {container} on {mb.base_url} (admin {ADMIN_EMAIL} / {admin_password}), "
                  f"servers pid {server.pid} and {tls_server.pid}; files in {work}")
        else:
            if started_container:
                subprocess.run(args=["docker", "rm", "-f", container], capture_output=True)
            tls_server.stop(keep_log=True)
            server.stop(keep_log=True)
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
