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
  9. the additional-options allowlist refuses a dangerous option;
 10. CSV uploads: a file with every upload column type, appended to, replaced, widened and queried;
 11. table transforms: created from a query-builder question and from SQL, run, run again, and dropped;
 12. the same as a user without the privileges (the server's refusal reaches the user by name), as a
     user with just the privileges needed, and uploads turned off again;
 13. upload dates and times with the Metabase JVM, the report time zone and the Hardcharger server in
     three different zones each, in three combinations (a fresh Metabase per combination).

It starts Hardcharger servers on free ports (plain HTTP with the sample data, TLS, one with access
control, and one per time-zone combination), and one Metabase container at a time named after this
process, and removes what it started. Metabase reaches the servers on the Docker host as
host.docker.internal.

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


def post_csv(mb: Metabase, path: str, filename: str, content: str, fields: dict | None = None):
    """POST a CSV file as multipart/form-data, as the browser's upload does. Returns (status, body, headers)."""
    boundary = "e2e" + secrets.token_hex(12)
    parts = b""
    for key, value in (fields or {}).items():
        parts += f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
    parts += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
              "Content-Type: text/csv\r\n\r\n").encode() + content.encode() + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(url=mb.base_url + path, data=parts, method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    request.add_header("X-Metabase-Session", mb.session)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            status, text, headers = response.status, response.read().decode(), dict(response.headers)
    except urllib.error.HTTPError as error:
        status, text, headers = error.code, error.read().decode(), dict(error.headers)
    try:
        body = json.loads(text) if text else None
    except json.JSONDecodeError:
        body = text
    return status, body, headers


def upload_csv(mb: Metabase, filename: str, content: str) -> dict:
    """Upload a CSV as a new table; returns the table Metabase made for it."""
    status, body, headers = post_csv(mb, "/api/upload/csv", filename, content, {"collection_id": "root"})
    if status != 200:
        raise Failure(f"uploading {filename} answered {status}: {json.dumps(body)[:1500]}")
    table_id = {k.lower(): v for k, v in headers.items()}.get("metabase-table-id")
    return mb.get(f"/api/table/{table_id}")


def native_rows(mb: Metabase, database_id: int, sql: str) -> list[list]:
    return mb.rows({"database": database_id, "type": "native", "native": {"query": sql}})


def run_transform(mb: Metabase, transform_id: int, deadline_seconds: float = 120.0) -> dict:
    """Run a transform and wait for the run to end; returns its last run."""
    before = (mb.get(f"/api/transform/{transform_id}").get("last_run") or {}).get("id")
    mb.post(f"/api/transform/{transform_id}/run")
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        last_run = mb.get(f"/api/transform/{transform_id}").get("last_run")
        if last_run and last_run.get("id") != before and last_run.get("status") not in ("started", "running"):
            return last_run
        time.sleep(1)
    raise Failure(f"transform {transform_id} did not finish within {deadline_seconds:.0f} s")


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
    engines = mb.get("/api/setting/engines")
    check("the hardcharger engine is registered", "hardcharger" in engines, sorted(engines))
    engine = engines["hardcharger"]
    check("its display name is Hardcharger", engine.get("driver-name") == "Hardcharger", engine.get("driver-name"))
    types = {p.get("name"): p.get("type") for p in engine["details-fields"]}
    for wanted in ["host", "port", "tls", "use-token", "user", "password", "catalog", "schema-filters-type",
                   "tls-skip-verify", "mtls-sign-in", "additional-options"]:
        check(f"the connection form has {wanted}", wanted in types, sorted(types))
    check("the password is a password field", types.get("password") == "password", types.get("password"))
    check("the access token is a secret", types.get("token-value") == "password", types.get("token-value"))
    for secret in ["tls-ca-certificate", "mtls-certificate", "mtls-key"]:
        check(f"{secret} is a secret (a file to upload, or a path on the server)",
              types.get(f"{secret}-value") == "textFile" and f"{secret}-path" in types, sorted(types))
    defaults = {p.get("name"): p.get("default") for p in engine["details-fields"]}
    check("TLS is on by default", defaults.get("tls") is True, defaults.get("tls"))


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

    # The first sync goes on to fingerprint the columns and scan their values.
    status_field = orders["status"]["id"]
    deadline = time.monotonic() + 120
    fingerprint, values = None, []
    while time.monotonic() < deadline:
        fingerprint = mb.get(f"/api/field/{orders['amount']['id']}").get("fingerprint")
        values = [v[0] for v in mb.get(f"/api/field/{status_field}/values").get("values", [])]
        if fingerprint and values:
            break
        time.sleep(2)
    check("a number column was fingerprinted",
          bool(fingerprint) and fingerprint["type"]["type/Number"]["min"] == 10
          and fingerprint["type"]["type/Number"]["max"] == 70, fingerprint)
    check("a category column's values were scanned", sorted(values) == sorted(STATUSES), values)

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
    seven = ORDERS[7]
    check("native SQL",
          [[r[0], r[1], float(r[2]), r[3]] for r in answer["data"]["rows"]]
          == [[7, seven["status"], float(seven["amount"]), "2026-01-01T21:00:00Z"]],
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
        # (Metabase sends whole numbers too big for JavaScript as text)
        row["c_bool"] is True and str(row["c_hugeint"]) == "12345678901234567890123456789"
        and str(row["c_uhugeint"]) == "12345678901234567890123456789"
        and str(row["c_ubigint"]) == "18446744073709551615" and float(row["c_dec"]) == 12345.678
        and row["c_dec0"] == 42 and row["c_blob"] == "0xAABB"
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
        # "Database type" is a searchable select
        engine_select = page.get_by_role("textbox", name="Database type")
        engine_select.click()
        engine_select.fill("Hardcharger")
        page.get_by_role("option", name="Hardcharger").click()
        expect(page.get_by_text("Use a secure connection (TLS)").first).to_be_visible(timeout=30_000)
        expect(page.get_by_text("Sign in with an access token").first).to_be_visible()
        expect(page.get_by_text("Catalog", exact=True).first).to_be_visible()
        check("the connection form shows the driver's fields", True)
        if screenshots:
            page.set_viewport_size({"width": 1100, "height": 1500})
            page.get_by_text("Add a database").first.scroll_into_view_if_needed()
            page.screenshot(path=str(screenshots / "connection-form.png"))
        page.get_by_text("Show advanced options").first.click()
        expect(page.get_by_text("Server CA certificate (PEM)").first).to_be_visible()
        expect(page.get_by_text("Sign in with the client certificate").first).to_be_visible()
        expect(page.get_by_text("Additional JDBC options").first).to_be_visible()
        check("and, under advanced options, the certificate fields and the additional options", True)

        # Where an admin turns uploads on: the Hardcharger database and its schemas are offered there.
        page.set_viewport_size({"width": 1300, "height": 900})
        page.goto(f"{mb.base_url}/admin/settings/uploads")
        page.wait_for_load_state("networkidle")
        page.get_by_role("textbox", name="Database to use for uploads").click()
        page.get_by_role("option", name="Hardcharger shop").click()
        page.get_by_role("textbox", name="Schema").click()
        expect(page.get_by_role("option", name="uploads")).to_be_visible(timeout=30_000)
        page.get_by_role("option", name="uploads").click()
        expect(page.get_by_role("button", name="Enable uploads")).to_be_enabled(timeout=30_000)
        check("the uploads settings offer the Hardcharger database and its schemas", True)
        if screenshots:
            page.screenshot(path=str(screenshots / "uploads-settings.png"))

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
    # Two guards: Metabase's own check knows the parameters this driver declares dangerous, and the driver's
    # allowlist refuses whatever is not on it.
    text = refused({**database_details(server), "additional-options": "oauthBrowser=/usr/bin/touch /tmp/pwned"})
    check("an option that names a program is refused", "dangerous" in text.lower(), text)
    text = refused({**database_details(server), "additional-options": "tlsRootCerts=/etc/passwd"})
    check("an option that names a file is refused", "dangerous" in text.lower(), text)
    text = refused({**database_details(server), "additional-options": "user=someone-else"})
    check("an option that is not on the allowlist is refused by name",
          "Unsupported additional JDBC option" in text and "user" in text and "someone-else" not in text, text)
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


def check_allowed_option(mb: Metabase, server: hardcharger.Server) -> None:
    print("9b. an allowed additional option")
    database = mb.post("/api/database", {
        "engine": "hardcharger", "name": "Hardcharger with zstd results",
        "details": database_details(server, catalog="memory",
                                    **{"advanced-options": True,
                                       "additional-options": "ipcCompression=zstd&resultPartitions=4"}),
    })
    # enough rows for several Arrow batches, so the result really is compressed and read in partitions
    rows = mb.rows({"database": database["id"], "type": "native",
                    "native": {"query": "SELECT count(*) AS n, sum(i) AS total FROM "
                                        "(SELECT i FROM range(500000) t(i) ORDER BY i)"}})
    check("a database with ipcCompression=zstd answers", rows == [[500000, 124999750000]], rows)
    answer = mb.dataset({"database": database["id"], "type": "native",
                         "native": {"query": "SELECT i, repeat('x', 20) AS pad FROM range(2000) t(i) ORDER BY i"}})
    check("zstd-compressed rows arrive whole and in order",
          [r[0] for r in answer["data"]["rows"]] == list(range(2000)), len(answer["data"]["rows"]))


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


# ---- uploads and transforms --------------------------------------------------------------------------------

LONG_TEXT = "a long text, " * 30  # more than 255 characters: Metabase's "text" upload type

# One column of every upload type Metabase detects, with the values that go wrong when something is
# careless: integers beyond 2^53, text with quotes, a line break and non-Latin characters, an empty row,
# a local time that does not exist in New York (02:30 on the day clocks go forward) and one that happens
# twice there, and instants written with three different offsets.
EVERY_TYPE_CSV = (
    "id,big,ratio,flag,on_date,at_local,at_offset,label,notes\n"
    '1,9223372036854775807,1234.5,true,2026-03-08,2026-03-08 02:30:00,2026-03-08T01:30:00-05:00,'
    '"Ann ""A"" O\'Neil","' + LONG_TEXT + '"\n'
    '2,-9007199254740993,-0.25,false,2026-11-01,2026-11-01 01:30:00,2026-07-01T12:00:00+05:30,'
    '"line one\nline two",short\n'
    "3,0,0.0,true,2024-02-29,2024-02-29 23:59:59,2026-01-15T23:59:59Z,日本語 🦆 ñ,\n"
    "4,,,,,,,,\n"
)
EVERY_TYPE_COLUMNS = [
    ["id", "BIGINT"], ["big", "BIGINT"], ["ratio", "DOUBLE"], ["flag", "BOOLEAN"], ["on_date", "DATE"],
    ["at_local", "TIMESTAMP"], ["at_offset", "TIMESTAMP WITH TIME ZONE"], ["label", "VARCHAR"], ["notes", "VARCHAR"],
]
# What the server must hold, read back in forms no time zone can change: whole numbers and dates as text,
# local times as written, instants as seconds since 1970.
EVERY_TYPE_ROWS = [
    [1, "9223372036854775807", 1234.5, True, "2026-03-08", "2026-03-08 02:30:00",
     int(dt.datetime(2026, 3, 8, 6, 30, tzinfo=dt.timezone.utc).timestamp()), 'Ann "A" O\'Neil', LONG_TEXT],
    [2, "-9007199254740993", -0.25, False, "2026-11-01", "2026-11-01 01:30:00",
     int(dt.datetime(2026, 7, 1, 6, 30, tzinfo=dt.timezone.utc).timestamp()), "line one\nline two", "short"],
    [3, "0", 0.0, True, "2024-02-29", "2024-02-29 23:59:59",
     int(dt.datetime(2026, 1, 15, 23, 59, 59, tzinfo=dt.timezone.utc).timestamp()), "日本語 🦆 ñ", None],
    [4, None, None, None, None, None, None, None, None],
]
READ_BACK = ("SELECT id, CAST(big AS VARCHAR), ratio, flag, CAST(on_date AS VARCHAR), "
             "strftime(at_local, '%Y-%m-%d %H:%M:%S'), CAST(epoch(at_offset) AS BIGINT), label, notes "
             'FROM uploads."{table}" ORDER BY id')


def columns_on_server(mb: Metabase, database_id: int, table_name: str) -> list[list]:
    return native_rows(mb, database_id,
                       "SELECT column_name, data_type FROM duckdb_columns() WHERE schema_name = 'uploads' "
                       f"AND table_name = '{table_name}' ORDER BY column_index")


def read_back(mb: Metabase, database_id: int, table_name: str) -> list[list]:
    rows = native_rows(mb, database_id, READ_BACK.format(table=table_name))
    return [[None if v is None else (float(v) if i == 2 else v) for i, v in enumerate(row)] for row in rows]


def check_uploads(mb: Metabase, database_id: int) -> None:
    print("10. CSV uploads")
    schemas = mb.get(f"/api/database/{database_id}/syncable_schemas")
    check("the schemas offered for uploads are this catalog's own", sorted(schemas) == ["main", "shop", "uploads"],
          schemas)
    mb.put("/api/setting/uploads-settings",
           {"value": {"db_id": database_id, "schema_name": "uploads", "table_prefix": "up_"}})

    table = upload_csv(mb, "every type.csv", EVERY_TYPE_CSV)
    name = table["name"]
    check("the upload made a table in the chosen schema, with the prefix",
          table["schema"] == "uploads" and name.startswith("up_every_type_") and table.get("is_upload") is True,
          (table["schema"], name, table.get("is_upload")))
    check("each column has the DuckDB type of its upload type",
          columns_on_server(mb, database_id, name) == EVERY_TYPE_COLUMNS, columns_on_server(mb, database_id, name))
    check("every value is stored exactly (big integers, quotes, line breaks, Unicode, empty cells)",
          read_back(mb, database_id, name) == EVERY_TYPE_ROWS, read_back(mb, database_id, name))
    fields = {f["name"]: f for f in mb.get(f"/api/table/{table['id']}/query_metadata")["fields"]}
    check("Metabase synced the new table's columns",
          {n: f["base_type"] for n, f in fields.items()} == {
              "id": "type/BigInteger", "big": "type/BigInteger", "ratio": "type/Float", "flag": "type/Boolean",
              "on_date": "type/Date", "at_local": "type/DateTime", "at_offset": "type/DateTimeWithLocalTZ",
              "label": "type/Text", "notes": "type/Text"},
          {n: f["base_type"] for n, f in fields.items()})
    rows = mb.rows({"database": database_id, "type": "query",
                    "query": {"source-table": table["id"], "aggregation": [["count"]],
                              "filter": ["=", ["field", fields["flag"]["id"], None], True]}})
    check("the uploaded table answers a query-builder question", rows == [[2]], rows)

    status, body, _ = post_csv(mb, f"/api/table/{table['id']}/append-csv", "more.csv",
                               "id,big,ratio,flag,on_date,at_local,at_offset,label,notes\n"
                               "5,5,5.5,false,2027-01-01,2027-01-01 00:00:00,2027-01-01T00:00:00+09:00,five,\n")
    check("appending a file adds its rows", status in (200, 204), (status, body))
    appended = read_back(mb, database_id, name)
    check("the table holds the old rows and the new one",
          appended[:4] == EVERY_TYPE_ROWS and appended[4] == [
              5, "5", 5.5, False, "2027-01-01", "2027-01-01 00:00:00",
              int(dt.datetime(2026, 12, 31, 15, 0, tzinfo=dt.timezone.utc).timestamp()), "five", None],
          appended[4:])

    status, body, _ = post_csv(mb, f"/api/table/{table['id']}/replace-csv", "new.csv",
                               "id,big,ratio,flag,on_date,at_local,at_offset,label,notes\n"
                               "9,9,9.0,true,2028-01-01,2028-01-01 09:09:09,2028-01-01T09:09:09Z,nine,n\n")
    check("replacing with a file swaps the rows", status in (200, 204), (status, body))
    check("only the new file's rows are left",
          read_back(mb, database_id, name) == [[
              9, "9", 9.0, True, "2028-01-01", "2028-01-01 09:09:09",
              int(dt.datetime(2028, 1, 1, 9, 9, 9, tzinfo=dt.timezone.utc).timestamp()), "nine", "n"]],
          read_back(mb, database_id, name))

    # a later file may widen a column, and bring a new one
    widening = upload_csv(mb, "widening.csv", "n,flag\n1,true\n2,false\n")
    status, body, _ = post_csv(mb, f"/api/table/{widening['id']}/append-csv", "wider.csv", "n,flag,extra\n2.5,7,hello\n")
    check("appending wider values is accepted", status in (200, 204), (status, body))
    check("the whole-number column became DOUBLE, the boolean BIGINT, and the new column was added",
          columns_on_server(mb, database_id, widening["name"]) == [["n", "DOUBLE"], ["flag", "BIGINT"], ["extra", "VARCHAR"]],
          columns_on_server(mb, database_id, widening["name"]))
    rows = native_rows(mb, database_id, f'SELECT n, flag, extra FROM uploads."{widening["name"]}" ORDER BY n')
    check("with the old values converted, not lost", rows == [[1.0, 1, None], [2.0, 0, None], [2.5, 7, "hello"]], rows)

    status, body, _ = post_csv(mb, f"/api/table/{widening['id']}/append-csv", "bad.csv", "n,flag,extra\nnot a number,1,x\n")
    check("a file that does not fit the table is refused, in words",
          status == 422 and "n" in json.dumps(body), (status, body))
    rows = native_rows(mb, database_id, f'SELECT count(*) FROM uploads."{widening["name"]}"')
    check("and leaves the table as it was", rows == [[3]], rows)


def check_transforms(mb: Metabase, database_id: int, tables: dict, fields: dict) -> None:
    print("11. table transforms")
    mb.put("/api/setting/transforms-enabled", {"value": True})

    def tables_in(schema: str) -> list[str]:
        return [r[0] for r in native_rows(mb, database_id,
                                          "SELECT table_name FROM information_schema.tables WHERE table_catalog = "
                                          f"current_database() AND table_schema = '{schema}' ORDER BY 1")]

    by_status = mb.post("/api/transform", {
        "name": "Orders by status",
        "source": {"type": "query", "query": {"database": database_id, "type": "query", "query": {
            "source-table": tables["orders"], "aggregation": [["count"]],
            "breakout": [["field", fields["orders.status"], None]]}}},
        # a schema that does not exist yet: the transform creates it
        "target": {"type": "table", "schema": "analytics", "name": "orders_by_status"},
    })
    last_run = run_transform(mb, by_status["id"])
    check("a transform from a query-builder question runs", last_run["status"] == "succeeded", last_run)
    rows = native_rows(mb, database_id, "SELECT status, count FROM analytics.orders_by_status ORDER BY status")
    check("its table has the query's rows", rows == [[s, 250] for s in sorted(STATUSES)], rows)

    doubled = mb.post("/api/transform", {
        "name": "Doubled",
        "source": {"type": "query", "query": {"database": database_id, "type": "native", "native": {
            "query": "SELECT status, count * 2 AS doubled FROM analytics.orders_by_status"}}},
        "target": {"type": "table", "schema": "analytics", "name": "orders_doubled"},
    })
    dependencies = [d["id"] for d in mb.get(f"/api/transform/{doubled['id']}/dependencies")]
    check("Metabase reads a SQL transform's tables out of its SQL (parsed as DuckDB)",
          dependencies == [by_status["id"]], dependencies)
    last_run = run_transform(mb, doubled["id"])
    check("a transform from SQL runs", last_run["status"] == "succeeded", last_run)
    rows = native_rows(mb, database_id, "SELECT status, doubled FROM analytics.orders_doubled ORDER BY status")
    check("its table has the query's rows too", rows == [[s, 500] for s in sorted(STATUSES)], rows)

    last_run = run_transform(mb, by_status["id"])
    check("running a transform again replaces its table", last_run["status"] == "succeeded", last_run)
    rows = native_rows(mb, database_id, "SELECT count(*), sum(count) FROM analytics.orders_by_status")
    check("with the same rows, not twice", [[int(v) for v in rows[0]]] == [[4, 1000]], rows)
    check("and leaves no temporary table behind", tables_in("analytics") == ["orders_by_status", "orders_doubled"],
          tables_in("analytics"))

    deadline = time.monotonic() + 60
    synced = None
    while time.monotonic() < deadline and not synced:
        synced = (mb.get(f"/api/transform/{by_status['id']}").get("table") or {}).get("id")
        time.sleep(1)
    check("Metabase synced the transform's table", bool(synced), synced)
    rows = mb.rows({"database": database_id, "type": "query",
                    "query": {"source-table": synced, "aggregation": [["count"]]}})
    check("which answers a query-builder question", rows == [[4]], rows)

    failing = mb.post("/api/transform", {
        "name": "Failing",
        "source": {"type": "query", "query": {"database": database_id, "type": "native", "native": {
            "query": "SELECT * FROM analytics.no_such_table"}}},
        "target": {"type": "table", "schema": "analytics", "name": "never_made"},
    })
    last_run = run_transform(mb, failing["id"])
    check("a transform whose SQL fails says why, in the server's words",
          last_run["status"] == "failed" and "no_such_table" in str(last_run.get("message")), last_run)
    check("and makes no table", "never_made" not in tables_in("analytics"), tables_in("analytics"))

    mb.call("DELETE", f"/api/transform/{doubled['id']}/table", ok=(200, 204))
    check("deleting a transform's table drops it on the server", tables_in("analytics") == ["orders_by_status"],
          tables_in("analytics"))
    for transform in (doubled, failing, by_status):
        if transform is by_status:
            mb.call("DELETE", f"/api/transform/{transform['id']}/table", ok=(200, 204))
        mb.call("DELETE", f"/api/transform/{transform['id']}", ok=(200, 204))
    check("and nothing of the transforms is left", tables_in("analytics") == [], tables_in("analytics"))


def check_privileges(mb: Metabase, acl_server: hardcharger.Server, viewer_password: str, uploader_password: str) -> None:
    print("12. without the privileges, and with just enough")

    def add(name: str, user: str, password: str) -> int:
        database = mb.post("/api/database", {
            "engine": "hardcharger", "name": name,
            "details": {"host": DOCKER_HOST_NAME, "port": acl_server.port, "tls": False, "use-token": False,
                        "user": user, "password": password, "catalog": "memory"}})
        wait_for_sync(mb, database["id"])
        return database["id"]

    viewer_db = add("Hardcharger as a viewer", "viewer", viewer_password)
    metadata = mb.get(f"/api/database/{viewer_db}/metadata?include_hidden=true")
    schemas = sorted({t["schema"] for t in metadata["tables"]})
    check("a read-only user's sync sees the tables it may read", schemas == ["shop"], schemas)
    rows = native_rows(mb, viewer_db, "SELECT HARDCHARGER_USER(), count(*) FROM shop.orders")
    check("and it can query them", rows == [["viewer", ORDER_COUNT]], rows)

    mb.put("/api/setting/uploads-settings", {"value": {"db_id": viewer_db, "schema_name": "uploads", "table_prefix": None}})
    status, body, _ = post_csv(mb, "/api/upload/csv", "refused.csv", "a,b\n1,2\n", {"collection_id": "root"})
    text = json.dumps(body)
    check("an upload without CREATE TABLE is refused with the server's reason",
          status >= 400 and "user viewer has no CREATE TABLE privilege on schema memory.uploads" in text,
          (status, text[:800]))
    check("the refusal contains no password", viewer_password not in text and acl_server.password not in text)
    rows = native_rows(mb, viewer_db, "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'uploads'")
    check("and nothing was created", rows == [[0]], rows)

    for target_schema, wanted in [("uploads", "user viewer has no CREATE TABLE privilege on schema memory.uploads"),
                                  ("brand_new", "user viewer has no CREATE SCHEMA privilege on catalog memory")]:
        transform = mb.post("/api/transform", {
            "name": f"Refused into {target_schema}",
            "source": {"type": "query", "query": {"database": viewer_db, "type": "native", "native": {
                "query": "SELECT status, count(*) AS n FROM shop.orders GROUP BY status"}}},
            "target": {"type": "table", "schema": target_schema, "name": "refused"},
        })
        last_run = run_transform(mb, transform["id"])
        check(f"a transform into {target_schema} without the privilege fails with the server's reason",
              last_run["status"] == "failed" and wanted in str(last_run.get("message")), last_run)
        mb.call("DELETE", f"/api/transform/{transform['id']}", ok=(200, 204))

    uploader_db = add("Hardcharger as an uploader", "uploader", uploader_password)
    mb.put("/api/setting/uploads-settings", {"value": {"db_id": uploader_db, "schema_name": "uploads", "table_prefix": None}})
    table = upload_csv(mb, "allowed.csv", "a,b\n1,x\n2,y\n")
    rows = native_rows(mb, uploader_db, f'SELECT a, b FROM uploads."{table["name"]}" ORDER BY a')
    check("with ALL PRIVILEGES on the uploads schema, and nothing else to write to, an upload works",
          rows == [[1, "x"], [2, "y"]], rows)
    status, body, _ = post_csv(mb, f"/api/table/{table['id']}/append-csv", "more.csv", "a,b\n3,z\n")
    check("and so does an append", status in (200, 204), (status, body))
    transform = mb.post("/api/transform", {
        "name": "Allowed",
        "source": {"type": "query", "query": {"database": uploader_db, "type": "native", "native": {
            "query": "SELECT status, count(*) AS n FROM shop.orders GROUP BY status"}}},
        "target": {"type": "table", "schema": "uploads", "name": "orders_by_status"},
    })
    for attempt in ("first", "second"):
        last_run = run_transform(mb, transform["id"])
        check(f"and a transform into that schema, {attempt} run", last_run["status"] == "succeeded", last_run)
    rows = native_rows(mb, uploader_db, "SELECT count(*), sum(n) FROM uploads.orders_by_status")
    check("whose table is right", [[int(v) for v in rows[0]]] == [[4, ORDER_COUNT]], rows)
    mb.call("DELETE", f"/api/transform/{transform['id']}/table", ok=(200, 204))
    mb.call("DELETE", f"/api/transform/{transform['id']}", ok=(200, 204))

    mb.put("/api/setting/uploads-settings", {"value": {"db_id": None, "schema_name": None, "table_prefix": None}})
    check("uploads can be turned off again", mb.get("/api/setting/uploads-settings").get("db_id") is None,
          mb.get("/api/setting/uploads-settings"))
    status, body, _ = post_csv(mb, "/api/upload/csv", "off.csv", "a\n1\n", {"collection_id": "root"})
    check("and then an upload is refused", status >= 400, (status, body))


# Three combinations in which the Metabase JVM, the Hardcharger server and Metabase's report time zone
# are all different, and every zone takes each role once. If any of the three leaked into how an upload's
# dates and times are stored, at least one combination would store something else.
ZONE_COMBINATIONS = [
    # (Metabase JVM, Hardcharger server, report time zone)
    ("UTC", "America/New_York", "Asia/Kolkata"),
    ("America/New_York", "Asia/Kolkata", "UTC"),
    ("Asia/Kolkata", "UTC", "America/New_York"),
]


def check_upload_zones(start_metabase, stop_metabase, binary: str | None, work: Path, admin_password: str) -> None:
    print("13. upload dates and times, in three combinations of zones")
    for jvm_zone, server_zone, report_zone in ZONE_COMBINATIONS:
        label = f"JVM {jvm_zone}, server {server_zone}, report {report_zone}"
        log = work / f"hardcharger-{server_zone.replace('/', '_')}.log"
        server = hardcharger.Server(
            host="0.0.0.0", binary=binary, log_file=log, env={"TZ": server_zone},
            extra_args=["--init-sql-commands", "CREATE SCHEMA uploads", "--no-web-ui"])
        server.start()
        try:
            mb = start_metabase(["-e", f"JAVA_TIMEZONE={jvm_zone}"])
            mb.set_up(password=admin_password)
            details = mb.get("/api/bug-reporting/details")
            actual_jvm_zone = details.get("system-info", {}).get("user.timezone")
            check(f"[{label}] the Metabase JVM runs in its zone", actual_jvm_zone == jvm_zone, actual_jvm_zone)
            database = mb.post("/api/database", {
                "engine": "hardcharger", "name": "Hardcharger zones",
                "details": database_details(server, catalog="memory")})
            synced = wait_for_sync(mb, database["id"])
            check(f"[{label}] the server runs in its zone", synced.get("timezone") == server_zone, synced.get("timezone"))
            mb.put("/api/setting/report-timezone", {"value": report_zone})
            rows = native_rows(mb, database["id"], "SELECT current_setting('TimeZone')")
            check(f"[{label}] the session runs in the report zone", rows == [[report_zone]], rows)

            mb.put("/api/setting/uploads-settings",
                   {"value": {"db_id": database["id"], "schema_name": "uploads", "table_prefix": None}})
            table = upload_csv(mb, "every type.csv", EVERY_TYPE_CSV)
            check(f"[{label}] column types", columns_on_server(mb, database["id"], table["name"]) == EVERY_TYPE_COLUMNS,
                  columns_on_server(mb, database["id"], table["name"]))
            stored = read_back(mb, database["id"], table["name"])
            check(f"[{label}] dates, local times and instants are stored as written",
                  stored == EVERY_TYPE_ROWS, [(a, b) for a, b in zip(stored, EVERY_TYPE_ROWS) if a != b])

            status, body, _ = post_csv(mb, f"/api/table/{table['id']}/append-csv", "more.csv",
                                       "id,big,ratio,flag,on_date,at_local,at_offset,label,notes\n"
                                       "5,5,5.5,false,2026-03-08,2026-03-08 02:30:00,2026-03-08T02:30:00+05:30,five,\n")
            check(f"[{label}] an append is accepted", status in (200, 204), (status, body))
            stored = read_back(mb, database["id"], table["name"])
            check(f"[{label}] and stored as written too",
                  stored[4] == [5, "5", 5.5, False, "2026-03-08", "2026-03-08 02:30:00",
                                int(dt.datetime(2026, 3, 7, 21, 0, tzinfo=dt.timezone.utc).timestamp()), "five", None],
                  stored[4])

            # and read back through the query builder, where the report zone decides how an instant is shown
            fields = {f["name"]: f["id"] for f in mb.get(f"/api/table/{table['id']}/query_metadata")["fields"]}
            rows = mb.rows({"database": database["id"], "type": "query", "query": {
                "source-table": table["id"],
                "fields": [["field", fields[n], None] for n in ("id", "on_date", "at_local", "at_offset")],
                "filter": ["<=", ["field", fields["id"], None], 3],
                "order-by": [["asc", ["field", fields["id"], None]]]}})
            zone = ZoneInfo(report_zone)
            shown_right = True
            for row, expected in zip(rows, EVERY_TYPE_ROWS):
                instant = dt.datetime.fromisoformat(row[3].replace("Z", "+00:00"))
                wanted = dt.datetime.fromtimestamp(expected[6], tz=dt.timezone.utc)
                shown_right = (shown_right and row[1][:10] == expected[4]
                               and row[2][:19].replace("T", " ") == expected[5]
                               and instant == wanted and instant.utcoffset() == wanted.astimezone(zone).utcoffset())
            check(f"[{label}] the query builder shows dates and local times as written, and instants in the report zone",
                  shown_right and len(rows) == 3, rows)
        finally:
            stop_metabase()
            server.stop(keep_log=True)


# ---- main ---------------------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jar", required=True, type=Path, help="the driver plugin jar to test")
    parser.add_argument("--metabase-image", default=os.environ.get("METABASE_IMAGE", DEFAULT_IMAGE))
    parser.add_argument("--screenshots", type=Path, help="directory to write the README's screenshots to")
    parser.add_argument("--keep", action="store_true",
                        help="after the main checks, leave Metabase and the servers running until interrupted")
    parser.add_argument("--no-browser", action="store_true", help="skip the browser step (not for the release gate)")
    parser.add_argument("--java-opts", help="JAVA_OPTS for Metabase (the default is none, as the README promises)")
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
    server_log, tls_log, acl_log = work / "hardcharger.log", work / "hardcharger-tls.log", work / "hardcharger-acl.log"
    container = f"mbhc-e2e-{os.getpid()}"
    admin_password = "Aa1!" + secrets.token_urlsafe(18)
    viewer_password, uploader_password = secrets.token_urlsafe(18), secrets.token_urlsafe(18)
    binary = os.environ.get("HARDCHARGER_SERVER_BINARY")
    running = {"container": False}
    failed = True

    def start_metabase(extra: list[str]) -> Metabase:
        """One Metabase container at a time, always under the same name."""
        port = free_port()
        run([
            "docker", "run", "-d", "--name", container,
            "--add-host", f"{DOCKER_HOST_NAME}:host-gateway",
            "-p", f"127.0.0.1:{port}:3000",
            "-v", f"{plugins}:/plugins",
            "-e", "MB_PLUGINS_DIR=/plugins",
            # No JAVA_OPTS: the README says the driver needs none.
            "-e", "MB_CHECK_FOR_UPDATES=false", "-e", "MB_ANON_TRACKING_ENABLED=false",
            *extra,
            args.metabase_image,
        ])
        running["container"] = True
        metabase = Metabase(base_url=f"http://127.0.0.1:{port}")
        metabase.wait_healthy(deadline_seconds=600, container=container)
        return metabase

    def metabase_log() -> str:
        logs = subprocess.run(args=["docker", "logs", container], capture_output=True, text=True)
        return logs.stdout + logs.stderr

    def stop_metabase() -> None:
        if running["container"]:
            subprocess.run(args=["docker", "rm", "-f", container], capture_output=True)
            running["container"] = False

    # 0.0.0.0: the Metabase container reaches the servers through the Docker host's gateway address.
    server = hardcharger.Server(
        host="0.0.0.0", binary=binary, log_file=server_log, env={"TZ": "UTC"},
        extra_args=["--init-sql-commands-file", str(HERE / "seed.sql"), "--no-web-ui", "--print-queries"],
    )
    tls_server = hardcharger.Server(
        host="0.0.0.0", binary=binary, log_file=tls_log, env={"TZ": "UTC"},
        tls_cert=certificates["server"], tls_key=certificates["server_key"],
        # A client certificate is accepted and can sign in, but is not demanded: the same server also takes
        # the password sign-in of the "TLS with a private CA" check.
        extra_args=["--no-web-ui", "--mtls-ca-cert", str(certificates["ca"]), "--no-mtls-require-client-cert",
                    "--mtls-certificate-sign-in", "--mtls-admin-identities", "metabase@example.invalid"],
    )
    # The same data under access control (users, roles and grants in a DuckDB file of the server's own).
    acl_sql = work / "seed-with-access-control.sql"
    acl_sql.write_text((HERE / "seed.sql").read_text() + "\n" + (HERE / "access-control.sql").read_text().format(
        viewer_password=viewer_password, uploader_password=uploader_password))
    acl_server = hardcharger.Server(
        host="0.0.0.0", binary=binary, log_file=acl_log, env={"TZ": "UTC"},
        extra_args=["--init-sql-commands-file", str(acl_sql), "--no-web-ui",
                    "--rbac-store-uri", f"duckdb:{work / 'access-control.duckdb'}"],
    )
    servers = [server, tls_server, acl_server]
    mb = None
    try:
        for each in servers:
            each.start()
        print(f"Hardcharger on :{server.port} (plain), :{tls_server.port} (TLS) and :{acl_server.port} "
              f"(access control); starting {args.metabase_image}")
        mb = start_metabase(["-e", f"JAVA_OPTS={args.java_opts}"] if args.java_opts else [])
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
        check_allowed_option(mb, server)
        check_tls(mb, tls_server, certificates)
        check_access_token(mb, server)
        check_uploads(mb, database_id)
        check_transforms(mb, database_id, tables, fields)
        check_privileges(mb, acl_server, viewer_password, uploader_password)

        log_text = metabase_log()
        secrets_used = [(server.password, "the database password"), (tls_server.password, "the TLS server's password"),
                        (acl_server.password, "the access-controlled server's password"),
                        (viewer_password, "the viewer's password"), (uploader_password, "the uploader's password")]
        for secret_value, what in secrets_used:
            check(f"Metabase's log does not contain {what}", secret_value not in log_text)
        check("the driver ran without a JVM option: no class-loading or native-library error in Metabase's log",
              "InaccessibleObjectException" not in log_text and "UnsatisfiedLinkError" not in log_text
              and "NoClassDefFoundError" not in log_text,
              [line for line in log_text.splitlines()
               if "Inaccessible" in line or "LinkError" in line or "NoClassDef" in line][:5])
        check("Metabase was told to parse this driver's SQL as DuckDB's",
              "Could not tell Metabase to parse Hardcharger" not in log_text and "Unknown dialect" not in log_text,
              [line for line in log_text.splitlines() if "dialect" in line.lower()][:5])
        if args.java_opts and "--sun-misc-unsafe-memory-access=allow" in args.java_opts:
            check("with --sun-misc-unsafe-memory-access=allow the JVM prints no sun.misc.Unsafe warning",
                  "sun.misc.Unsafe" not in log_text)
        elif "sun.misc.Unsafe" in log_text:
            # JDK 24+ prints this once for Arrow's memory access; it is a warning, and the README says so.
            print("  note: the JVM printed its one-time sun.misc.Unsafe warning (Arrow, in the JDBC driver)")
        for log_path in (server_log, tls_log, acl_log):
            text = log_path.read_text(errors="replace")
            check(f"{log_path.name} does not contain a password",
                  all(secret_value not in text for secret_value, _ in secrets_used))

        if args.keep:
            print(f"left running: container {container} on {mb.base_url} (admin {ADMIN_EMAIL} / {admin_password}), "
                  f"Hardcharger on :{server.port} (password {server.password}), :{tls_server.port} and "
                  f":{acl_server.port}; files in {work}. Interrupt (Ctrl-C) to go on.", flush=True)
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
        stop_metabase()

        check_upload_zones(start_metabase, stop_metabase, binary, work, admin_password)

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
        if failed and running["container"]:
            print("---- last Metabase log lines ----", file=sys.stderr)
            print(metabase_log()[-12000:], file=sys.stderr)
            for log_path in sorted(work.glob("hardcharger*.log")):
                print(f"---- {log_path.name} (tail) ----", file=sys.stderr)
                print("\n".join(log_path.read_text(errors="replace").splitlines()[-25:]), file=sys.stderr)
        stop_metabase()
        for each in reversed(servers):
            each.stop(keep_log=True)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
