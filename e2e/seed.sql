-- Sample data for the end-to-end check. Everything is derived from row numbers, so e2e.py computes
-- the answers it expects from the same formulas and compares them with what Metabase returns.

CREATE SCHEMA shop;

CREATE TABLE shop.customers (
  id        INTEGER PRIMARY KEY,
  name      VARCHAR NOT NULL,
  email     VARCHAR NOT NULL UNIQUE,
  region    VARCHAR NOT NULL,
  joined_on DATE    NOT NULL
);
COMMENT ON TABLE shop.customers IS 'People who buy things';
COMMENT ON COLUMN shop.customers.region IS 'Sales region';

INSERT INTO shop.customers
SELECT i,
       'Customer ' || i,
       'customer' || i || '@example.invalid',
       ['North', 'South', 'East', 'West'][(i % 4) + 1],
       DATE '2025-01-01' + CAST(i AS INTEGER)
FROM range(1, 51) t(i);

CREATE TABLE shop.orders (
  id          INTEGER PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES shop.customers (id),
  status      VARCHAR NOT NULL,
  quantity    INTEGER NOT NULL,
  amount      DECIMAL(12,2) NOT NULL,
  created_at  TIMESTAMP WITH TIME ZONE NOT NULL
);

-- Order i: one every 3 hours from 2026-01-01 00:00 UTC.
INSERT INTO shop.orders
SELECT i,
       (i % 50) + 1,
       ['new', 'paid', 'shipped', 'cancelled'][(i % 4) + 1],
       (i % 5) + 1,
       CAST(((i % 7) + 1) * 10 AS DECIMAL(12,2)),
       TIMESTAMPTZ '2026-01-01 00:00:00+00' + i * INTERVAL 3 HOUR
FROM range(0, 1000) t(i);

CREATE VIEW shop.paid_orders AS SELECT * FROM shop.orders WHERE status = 'paid';

-- One row of every DuckDB type the driver maps, for the sync check.
CREATE TYPE shop.mood AS ENUM ('sad', 'ok', 'happy');
CREATE TABLE shop.all_types (
  c_bool BOOLEAN, c_tinyint TINYINT, c_smallint SMALLINT, c_int INTEGER, c_bigint BIGINT, c_hugeint HUGEINT,
  c_utinyint UTINYINT, c_usmallint USMALLINT, c_uint UINTEGER, c_ubigint UBIGINT, c_uhugeint UHUGEINT,
  c_float FLOAT, c_double DOUBLE, c_dec DECIMAL(18,3), c_dec0 DECIMAL(18,0),
  c_varchar VARCHAR, c_blob BLOB, c_uuid UUID,
  c_date DATE, c_time TIME, c_timetz TIMETZ,
  c_ts TIMESTAMP, c_ts_s TIMESTAMP_S, c_ts_ms TIMESTAMP_MS, c_ts_ns TIMESTAMP_NS, c_tstz TIMESTAMPTZ,
  c_interval INTERVAL, c_enum shop.mood,
  c_list INTEGER[], c_struct STRUCT(a INTEGER, b VARCHAR), c_map MAP(VARCHAR, INTEGER), c_json JSON,
  c_variant VARIANT
);
INSERT INTO shop.all_types VALUES (
  true, 1, 2, 3, 4, 12345678901234567890123456789,
  5, 6, 7, 18446744073709551615, 12345678901234567890123456789,
  1.5, 2.5, 12345.678, 42,
  'hello', '\xAA\xBB'::BLOB, '123e4567-e89b-12d3-a456-426614174000',
  DATE '2026-11-13', TIME '13:14:15.123456', TIMETZ '13:14:15+02:00',
  TIMESTAMP '2026-11-13 13:14:15.123456', TIMESTAMP_S '2026-11-13 13:14:15', TIMESTAMP_MS '2026-11-13 13:14:15.123',
  TIMESTAMP_NS '2026-11-13 13:14:15.123456789', TIMESTAMPTZ '2026-11-13 13:14:15.123456+00',
  INTERVAL '1 year 2 months 3 days 4 hours', 'ok',
  [1, 2, 3], {'a': 1, 'b': 'x'}, MAP {'k': 1}, '{"a": [1, 2]}',
  42::VARIANT
);
